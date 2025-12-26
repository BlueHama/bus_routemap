import xml.etree.ElementTree as elemtree
from datetime import datetime
import requests, time, sys, os, re, math, json, base64, urllib, io
import mapbox
from routemap import convert_gps, convert_pos, Mapframe, RouteMap
from concurrent.futures import ThreadPoolExecutor, as_completed

class ApiKeyError(Exception):
    pass

class ServerError(Exception):
    pass

class SeoulApiKeyError(ApiKeyError):
    pass

class GyeonggiApiKeyError(ApiKeyError):
    pass

class BusanApiKeyError(ApiKeyError):
    pass

# [신규] TAGO API용 예외 클래스
class TagoApiKeyError(ApiKeyError):
    pass

route_type_str = {0: '공용', 1: '공항', 2: '마을', 3: '간선', 4: '지선', 5: '순환', 6: '광역', 7: '인천', 8: '경기', 9: '폐지', 10: '투어',
    11: '직행', 12: '좌석', 13: '일반', 14: '광역', 15: '따복', 16: '순환', 21: '농어촌직행', 22: '농어촌좌석', 23: '농어촌', 30: '마을', 
    41: '고속', 42: '시외좌석', 43: '시외일반', 51: '공항리무진', 52: '공항좌석', 53: '공항일반',
    61: '일반', 62: '급행', 63: '좌석', 64: '심야', 65: '마을'}

cache_dir = 'cache'

def convert_busan_bus_type(type_str):
    if type_str[:2] == '일반':
        return 61
    elif type_str[:2] == '급행':
        return 62
    elif type_str[:2] == '좌석':
        return 63
    elif type_str[:2] == '심야':
        return 64
    elif type_str[:2] == '마을':
        return 65
    else:
        return 0

def convert_tago_bus_type(type_str):
    if not type_str:
        return 0
    
    type_map = {
        '간선': 3, 
        '지선': 4, 
        '마을': 2, 
        '광역': 6,
        '직행': 11, 
        '좌석': 12, 
        '일반': 13,
        '공항': 1, 
        '순환': 5, 
        '급행': 62,
        '심야': 64,
        '시외': 43,
        '농어촌': 23,
    }
    
    # type_str에서 '버스' 단어 제거 (예: '간선버스' -> '간선')
    type_key = type_str.replace('버스', '').strip()
    
    # 부분 일치 검색
    for key, value in type_map.items():
        if key in type_key:
            return value
    
    return 0  # 맵에 없으면 '공용'(0)으로 처리

def convert_type_to_region(route_type, route_id=None):
    # TAGO API 버스인 경우 ID에서 도시 이름 추출
    if route_id and isinstance(route_id, str) and route_id.startswith('TAGO|'):
        parts = route_id.split('|')
        if len(parts) >= 3:
            city_code = parts[1]
            # 도시 코드를 도시 이름으로 변환 (캐시 사용)
            city_name = get_city_name_from_code(city_code)
            if city_name:
                return city_name
    
    # 기존 로직
    if route_type <= 10:
        return '서울'
    elif route_type <= 60:
        return '경기'
    else:
        return '부산'

def check_seoul_key_valid(key):
    params = {'serviceKey': key}
    
    route_api_res = requests.get('http://ws.bus.go.kr/api/rest/busRouteInfo/getStaionByRoute', params = params).text
    route_api_tree = elemtree.fromstring(route_api_res)

    api_err = int(route_api_tree.find('./msgHeader/headerCd').text)
    
    if api_err == 7:
        return False
    return True

def check_gyeonggi_key_valid(key):
    params = {'serviceKey': key}
    route_api_res = requests.get('http://apis.data.go.kr/6410000/busrouteservice/v2/getBusRouteStationListv2', params = params, timeout = 20)

    if route_api_res.headers.get('Content-Type').startswith('text/xml'):
        route_api_tree = elemtree.fromstring(route_api_res.text)
        
        api_err = route_api_tree.find('./cmmMsgHeader/returnAuthMsg')
        if api_err != None:
            return False
    return True


def check_busan_key_valid(key):
    params = {'serviceKey': key}
    route_api_res = requests.get('https://apis.data.go.kr/6260000/BusanBIMS/busInfoByRouteId', params = params, timeout = 20).text
    route_api_tree = elemtree.fromstring(route_api_res)
    
    api_err = route_api_tree.find('./cmmMsgHeader/returnAuthMsg')
    if api_err != None:
        return False
    return True

# [신규] TAGO API 키 유효성 검사
def check_tago_key_valid(key):
    # 인천광역시(23), 1번 버스로 테스트
    params = {'serviceKey': key, 'cityCode': '23', 'routeNo': '1', '_type': 'xml'}
    
    try:
        route_api_res = requests.get('http://apis.data.go.kr/1613000/BusRouteInfoInqireService/getBusRouteList', params = params, timeout = 20)

        if route_api_res.headers.get('Content-Type').startswith('text/xml'):
            route_api_tree = elemtree.fromstring(route_api_res.text)
            
            # TAGO API는 에러 코드를 header/resultCode 에 반환
            # 03 = SERVICE_KEY_IS_NOT_REGISTERED_ERROR
            api_err_code = route_api_tree.find('./header/resultCode')
            if api_err_code is not None and api_err_code.text == '03':
                return False
        elif 'SERVICE KEY IS NOT REGISTERED' in route_api_res.text: # XML이 아닌 에러
             return False
        return True
    except Exception:
        return False

# ... (기존 get_seoul_bus_stops, get_gyeonggi_bus_stops, get_busan_bus_stops 함수는 변경 없음) ...
def get_seoul_bus_stops(key, routeid):
    # 서울 버스 정류장 목록 조회
    params = {'serviceKey': key, 'busRouteId': routeid}
    
    route_api_res = requests.get('http://ws.bus.go.kr/api/rest/busRouteInfo/getStaionByRoute', params = params).text
    route_api_tree = elemtree.fromstring(route_api_res)

    api_err = int(route_api_tree.find('./msgHeader/headerCd').text)
    
    if api_err == 7:
        raise SeoulApiKeyError()
    
    if api_err != 0 and api_err != 4:
        raise ValueError(route_api_tree.find('./msgHeader/headerMsg').text)

    route_api_body = route_api_tree.find('./msgBody')
    bus_stop_items = route_api_body.findall('./itemList')

    bus_stops = []
    for i in bus_stop_items:
        stop = {}
        stop['arsid'] = i.find('./arsId').text
        stop['name'] = i.find('./stationNm').text
        stop['pos'] = (float(i.find('./gpsX').text), float(i.find('./gpsY').text))
        stop['is_trans'] = i.find('./transYn').text == 'Y'
        
        bus_stops.append(stop)
    
    return bus_stops

def get_gyeonggi_bus_stops(key, routeid):
    # 경기 버스 정류장 목록 조회
    params = {'serviceKey': key, 'routeId': routeid, 'format': 'xml'}
    
    route_api_res = requests.get('http://apis.data.go.kr/6410000/busrouteservice/v2/getBusRouteStationListv2', params = params, timeout = 20)
    if not (route_api_res.headers.get('Content-Type').startswith('text/xml') or route_api_res.headers.get('Content-Type').startswith('application/xml')):
        return []

    route_api_tree = elemtree.fromstring(route_api_res.text)
    
    api_common_err = route_api_tree.find('./cmmMsgHeader/returnAuthMsg')
    if api_common_err != None:
        raise GyeonggiApiKeyError(api_common_err.text)

    api_err = int(route_api_tree.find('./msgHeader/resultCode').text)

    if api_err != 0 and api_err != 4:
        raise ValueError(route_api_tree.find('./msgHeader/resultMessage').text)

    route_api_body = route_api_tree.find('./msgBody')
    bus_stop_items = route_api_body.findall('./busRouteStationList')

    bus_stops = []
    for i in bus_stop_items:
        stop = {}
        
        arsid_find = i.find('./mobileNo')
        if arsid_find:
            stop['arsid'] = arsid_find.text
        else:
            stop['arsid'] = None
        
        stop['name'] = i.find('./stationName').text
        stop['pos'] = (float(i.find('./x').text), float(i.find('./y').text))
        stop['is_trans'] = i.find('./turnYn').text == 'Y'
        
        bus_stops.append(stop)
    
    return bus_stops

def get_busan_bus_stops(key, route_id, route_bims_id):
    # 부산 버스 정류장 목록 조회
    params = {'optBusNum': route_bims_id}
    
    route_api_res = requests.get('http://bus.busan.go.kr/busanBIMS/Ajax/busLineList.asp', params = params, timeout = 20).text
    route_api_tree = elemtree.fromstring(route_api_res)

    bus_stop_items = route_api_tree.findall('./line')
    
    bus_stops = []
    for i in bus_stop_items[2:]:
        stop = {}
        
        stop['arsid'] = i.attrib['text4']
        stop['name'] = i.attrib['text1']
        stop['pos'] = (float(i.attrib['text2']), float(i.attrib['text3']))
        stop['is_trans'] = False
        
        bus_stops.append(stop)
    
    params2 = {'serviceKey': key, 'lineid': route_id}
    route_api_res2 = requests.get('https://apis.data.go.kr/6260000/BusanBIMS/busInfoByRouteId', params = params2, timeout = 20).text
    route_api_tree2 = elemtree.fromstring(route_api_res2)
    
    api_common_err = route_api_tree2.find('./cmmMsgHeader/returnAuthMsg')
    if api_common_err != None:
        raise BusanApiKeyError(api_common_err.text)
    
    bus_stop_items2 = route_api_tree2.findall('./body/items/item')
    for i in bus_stop_items2:
        if i.find('./rpoint').text == '1':
            bus_stops[int(i.find('./bstopidx').text) - 1]['is_trans'] = True
            break
    
    return bus_stops

# [신규] TAGO API로 버스 정류장 목록 조회
def get_tago_bus_stops(key, routeid, cityCode):
    bus_stops = []
    page_no = 1
    num_of_rows = 100  # 충분히 크게 설정
    
    while True:
        params = {
            'serviceKey': key, 
            'routeId': routeid, 
            'cityCode': cityCode, 
            'pageNo': page_no,
            'numOfRows': num_of_rows,
            '_type': 'xml'
        }
        
        try:
            route_api_res = requests.get(
                'https://apis.data.go.kr/1613000/BusRouteInfoInqireService/getRouteAcctoThrghSttnList', 
                params=params, 
                timeout=20
            )
            
            if not (route_api_res.headers.get('Content-Type', '').startswith('text/xml') or 
                    route_api_res.headers.get('Content-Type', '').startswith('application/xml')):
                break

            route_api_tree = elemtree.fromstring(route_api_res.text)
            
            api_err_code = route_api_tree.find('./header/resultCode')
            if api_err_code is None:
                raise TagoApiKeyError("TAGO API 응답 형식이 올바르지 않습니다.")
            
            if api_err_code.text == '03':  # SERVICE_KEY_IS_NOT_REGISTERED_ERROR
                raise TagoApiKeyError(route_api_tree.find('./header/resultMsg').text)

            if api_err_code.text != '00':  # 00: 정상
                if api_err_code.text == '04':  # NODATA_ERROR
                    break
                raise ValueError(route_api_tree.find('./header/resultMsg').text)

            route_api_body = route_api_tree.find('./body/items')
            if route_api_body is None:
                break
                
            bus_stop_items = route_api_body.findall('./item')
            
            if not bus_stop_items:
                break

            for i in bus_stop_items:
                stop = {}
                
                # 정류소 번호
                arsid_find = i.find('./nodeno')
                stop['arsid'] = arsid_find.text if arsid_find is not None else None
                
                # 정류장 이름
                nodenm_elem = i.find('./nodenm')
                stop['name'] = nodenm_elem.text if nodenm_elem is not None else ''
                
                # 좌표 (경도, 위도)
                gpslong_elem = i.find('./gpslong')
                gpslati_elem = i.find('./gpslati')
                if gpslong_elem is not None and gpslati_elem is not None:
                    stop['pos'] = (float(gpslong_elem.text), float(gpslati_elem.text))
                else:
                    stop['pos'] = (0.0, 0.0)
                
                # updowncd 저장 (중요!)
                updown_elem = i.find('./updowncd')
                stop['updown_cd'] = updown_elem.text if updown_elem is not None else '0'
                
                # is_trans는 일단 False로 초기화 (나중에 설정)
                stop['is_trans'] = False
                
                bus_stops.append(stop)
            
            # 페이지당 행 수보다 적게 반환되면 마지막 페이지
            if len(bus_stop_items) < num_of_rows:
                break
                
            page_no += 1
            
        except TagoApiKeyError:
            raise
        except requests.exceptions.Timeout:
            print(f"TAGO API 타임아웃 (페이지 {page_no})")
            break
        except Exception as e:
            print(f"TAGO API 정류장 조회 오류 (페이지 {page_no}): {str(e)}")
            break
    
    # 모든 정류장을 수집한 후 회차지 판단
    for idx in range(len(bus_stops) - 1):
        current_updown = bus_stops[idx].get('updown_cd', '0')
        next_updown = bus_stops[idx + 1].get('updown_cd', '0')
        
        # 현재가 0이고 다음이 1이면 현재가 회차지
        if current_updown == '0' and next_updown == '1':
            bus_stops[idx]['is_trans'] = True
            break  # 회차지는 하나만 있으므로 찾으면 종료
    
    return bus_stops


# ... (기존 get_seoul_bus_type, get_gyeonggi_bus_type, get_busan_bus_type 함수는 변경 없음) ...
def get_seoul_bus_type(key, routeid):
    # 서울 버스 노선정보 조회
    params = {'serviceKey': key, 'busRouteId': routeid}
    
    route_api_res = requests.get('http://ws.bus.go.kr/api/rest/busRouteInfo/getRouteInfo', params = params).text
    route_api_tree = elemtree.fromstring(route_api_res)

    api_err = int(route_api_tree.find('./msgHeader/headerCd').text)
    
    if api_err == 7:
        raise SeoulApiKeyError()

    if api_err != 0 and api_err != 4:
        raise ValueError(route_api_tree.find('./msgHeader/headerMsg').text)

    route_api_body = route_api_tree.find('./msgBody/itemList')
    route_info = {}
    
    route_info['type'] = int(route_api_body.find('./routeType').text)
    route_info['name'] = route_api_body.find('./busRouteNm').text
    route_info['start'] = route_api_body.find('./stStationNm').text
    route_info['end'] = route_api_body.find('./edStationNm').text
    
    return route_info

def get_gyeonggi_bus_type(key, routeid):
    # 경기 버스 노선정보 조회
    params = {'serviceKey': key, 'routeId': routeid, 'format': 'xml'}
    
    route_api_res = requests.get('http://apis.data.go.kr/6410000/busrouteservice/v2/getBusRouteInfoItemv2', params = params, timeout = 20)
    if not (route_api_res.headers.get('Content-Type').startswith('text/xml') or route_api_res.headers.get('Content-Type').startswith('application/xml')):
        return []

    route_api_tree = elemtree.fromstring(route_api_res.text)

    api_common_err = route_api_tree.find('./cmmMsgHeader/returnAuthMsg')
    if api_common_err != None:
        raise GyeonggiApiKeyError(api_common_err.text)

    api_err = int(route_api_tree.find('./msgHeader/resultCode').text)

    if api_err != 0 and api_err != 4:
        raise ValueError(route_api_tree.find('./msgHeader/resultMessage').text)

    route_api_body = route_api_tree.find('./msgBody/busRouteInfoItem')
    route_info = {}
    
    route_info['type'] = int(route_api_body.find('./routeTypeCd').text)
    route_info['name'] = route_api_body.find('./routeName').text
    route_info['start'] = route_api_body.find('./startStationName').text
    route_info['end'] = route_api_body.find('./endStationName').text
    
    return route_info

def get_busan_bus_type(key, route_bims_id):
    # 부산 버스 노선정보 조회
    params = {'optBusNum': route_bims_id}
    
    route_api_res = requests.get('http://bus.busan.go.kr/busanBIMS/Ajax/busLineList.asp', params = params, timeout = 20).text
    route_api_tree = elemtree.fromstring(route_api_res)

    bus_stop_items = route_api_tree.findall('./line')
    
    bus_info_tree = bus_stop_items[0]
    route_info = {}
    
    route_info['type'] = convert_busan_bus_type(bus_info_tree.attrib['text2'])
    route_info['name'] = bus_info_tree.attrib['text1']
    route_info['start'] = bus_info_tree.attrib['text3']
    route_info['end'] = bus_info_tree.attrib['text4']
    
    return route_info

# [신규] TAGO API로 버스 노선정보 조회
def get_tago_bus_type(key, routeid, cityCode):
    params = {'serviceKey': key, 'routeId': routeid, 'cityCode': cityCode, '_type': 'xml'}
    
    route_api_res = requests.get('https://apis.data.go.kr/1613000/BusRouteInfoInqireService/getRouteInfoIem', params = params, timeout = 20)
    
    if not (route_api_res.headers.get('Content-Type').startswith('text/xml') or route_api_res.headers.get('Content-Type').startswith('application/xml')):
        return []

    route_api_tree = elemtree.fromstring(route_api_res.text)

    api_err_code = route_api_tree.find('./header/resultCode')
    if api_err_code is None:
         raise TagoApiKeyError("TAGO API 응답 형식이 올바르지 않습니다.")
    
    if api_err_code.text == '03': # SERVICE_KEY_IS_NOT_REGISTERED_ERROR
        raise TagoApiKeyError(route_api_tree.find('./header/resultMsg').text)

    if api_err_code.text != '00': # 00: 정상
        raise ValueError(route_api_tree.find('./header/resultMsg').text)

    route_api_body = route_api_tree.find('./body/items/item')
    if route_api_body is None:
        return {}  # ← 빈 딕셔너리 반환
        
    route_info = {}
    
    # type, name, start, end가 None일 수 있음
    routetp_elem = route_api_body.find('./routetp')
    routeno_elem = route_api_body.find('./routeno')
    startnodenm_elem = route_api_body.find('./startnodenm')
    endnodenm_elem = route_api_body.find('./endnodenm')
    
    route_info['type'] = convert_tago_bus_type(routetp_elem.text) if routetp_elem is not None else 0
    route_info['name'] = routeno_elem.text if routeno_elem is not None else ''
    route_info['start'] = startnodenm_elem.text if startnodenm_elem is not None else ''
    route_info['end'] = endnodenm_elem.text if endnodenm_elem is not None else ''
    
    return route_info


# ... (기존 get_seoul_bus_route, get_gyeonggi_bus_route, get_busan_bus_route 함수는 변경 없음) ...
def get_seoul_bus_route(key, routeid):
    # 서울 버스 노선형상 조회
    params = {'serviceKey': key, 'busRouteId': routeid}
    
    route_api_res = requests.get('http://ws.bus.go.kr/api/rest/busRouteInfo/getRoutePath', params = params).text
    route_api_tree = elemtree.fromstring(route_api_res)

    api_err = int(route_api_tree.find('./msgHeader/headerCd').text)

    if api_err == 7:
        raise SeoulApiKeyError()

    if api_err != 0 and api_err != 4:
        raise ValueError(route_api_tree.find('./msgHeader/headerMsg').text)

    route_api_body = route_api_tree.find('./msgBody')
    xml_route_positions = route_api_body.findall('./itemList')

    route_positions = []

    for i in xml_route_positions:
        x = float(i.find('./gpsX').text)
        y = float(i.find('./gpsY').text)
        
        route_positions.append((x, y))
    
    return route_positions

def get_gyeonggi_bus_route(key, routeid):
    # 경기 버스 노선형상 조회
    params = {'serviceKey': key, 'routeId': routeid, 'format': 'xml'}
    
    route_api_res = requests.get('http://apis.data.go.kr/6410000/busrouteservice/v2/getBusRouteLineListv2', params = params, timeout = 20)
    if not (route_api_res.headers.get('Content-Type').startswith('text/xml') or route_api_res.headers.get('Content-Type').startswith('application/xml')):
        return []
    
    route_api_tree = elemtree.fromstring(route_api_res.text)
    
    api_common_err = route_api_tree.find('./cmmMsgHeader/returnAuthMsg')
    if api_common_err != None:
        raise GyeonggiApiKeyError(api_common_err.text)

    api_err = int(route_api_tree.find('./msgHeader/resultCode').text)

    if api_err != 0 and api_err != 4:
        raise ValueError(route_api_tree.find('./msgHeader/resultMessage').text)

    route_api_body = route_api_tree.find('./msgBody')
    xml_route_positions = route_api_body.findall('./busRouteLineList')

    route_positions = []

    for i in xml_route_positions:
        x = float(i.find('./x').text)
        y = float(i.find('./y').text)
        
        route_positions.append((x, y))
    
    return route_positions

def get_busan_bus_route(route_name):
    # 부산 버스 노선형상 조회
    params = {'busLineId': route_name}
    encoded_params = urllib.parse.urlencode(params, encoding='cp949')
    
    route_api_res = requests.get('http://bus.busan.go.kr/busanBIMS/Ajax/busLineCoordList.asp?' + encoded_params, timeout = 5).text
    route_api_tree = elemtree.fromstring(route_api_res)
    xml_route_positions = route_api_tree.findall('./coord')
    
    if not xml_route_positions:
        return None, None
    
    route_bims_id = xml_route_positions[0].attrib['value1']
    route_positions = []

    for i in xml_route_positions[1:]:
        x = float(i.attrib['value2'])
        y = float(i.attrib['value3'])
        
        route_positions.append((x, y))
    
    return route_positions, route_bims_id

# [신규] TAGO API로 버스 노선형상 조회
def get_tago_bus_route(key, routeid, cityCode):
    route_positions = []
    page_no = 1
    num_of_rows = 100  # 한 페이지당 최대 행 수
    
    while True:
        params = {
            'serviceKey': key, 
            'routeId': routeid, 
            'cityCode': cityCode,
            'pageNo': page_no,
            'numOfRows': num_of_rows,
            '_type': 'xml'
        }
        
        try:
            route_api_res = requests.get(
                'https://apis.data.go.kr/1613000/BusRouteInfoInqireService/getRouteAcctoThrghSttnList', 
                params=params, 
                timeout=20
            )
            
            if not (route_api_res.headers.get('Content-Type').startswith('text/xml') or 
                    route_api_res.headers.get('Content-Type').startswith('application/xml')):
                break
            
            route_api_tree = elemtree.fromstring(route_api_res.text)
            
            api_err_code = route_api_tree.find('./header/resultCode')
            if api_err_code is None:
                raise TagoApiKeyError("TAGO API 응답 형식이 올바르지 않습니다.")
            
            if api_err_code.text == '03':  # SERVICE_KEY_IS_NOT_REGISTERED_ERROR
                raise TagoApiKeyError(route_api_tree.find('./header/resultMsg').text)

            if api_err_code.text != '00':  # 00: 정상
                if api_err_code.text == '04':  # NODATA_ERROR
                    break
                raise ValueError(route_api_tree.find('./header/resultMsg').text)

            route_api_body = route_api_tree.find('./body/items')
            if route_api_body is None:
                break
                
            xml_route_positions = route_api_body.findall('./item')
            
            if not xml_route_positions:
                break

            for i in xml_route_positions:
                x = float(i.find('./gpslong').text)
                y = float(i.find('./gpslati').text)
                route_positions.append((x, y))
            
            # 페이지당 행 수보다 적게 반환되면 마지막 페이지
            if len(xml_route_positions) < num_of_rows:
                break
                
            page_no += 1
            
        except requests.exceptions.Timeout:
            print(f"TAGO API 타임아웃 (페이지 {page_no})")
            break
        except Exception as e:
            print(f"TAGO API 노선형상 조회 오류 (페이지 {page_no}): {str(e)}")
            break
    
    return route_positions


# ... (기존 search_seoul_bus_info, search_gyeonggi_bus_info, search_busan_bus_info 함수는 변경 없음) ...
def search_seoul_bus_info(key, number):
    params = {'serviceKey': key, 'strSrch': number}
    
    list_api_res = requests.get('http://ws.bus.go.kr/api/rest/busRouteInfo/getBusRouteList', params = params).text
    list_api_tree = elemtree.fromstring(list_api_res)

    api_err = int(list_api_tree.find('./msgHeader/headerCd').text)
    
    if api_err == 7:
        raise SeoulApiKeyError(list_api_tree.find('./msgHeader/headerMsg').text)

    if api_err != 0 and api_err != 4:
        raise ValueError(list_api_tree.find('./msgHeader/headerMsg').text)

    list_api_body = list_api_tree.find('./msgBody')
    xml_bus_list = list_api_body.findall('./itemList')

    bus_info_list = []

    for i in xml_bus_list:
        name = i.find('./busRouteNm').text
        route_id = i.find('./busRouteId').text
        start = i.find('./stStationNm').text
        end = i.find('./edStationNm').text
        route_type = int(i.find('./routeType').text)
        
        if route_type == 7 or route_type == 8:
            continue
        
        bus_info_list.append({'name': name, 'id': route_id, 'desc': start + '~' + end, 'type': route_type})
    
    return bus_info_list

def search_gyeonggi_bus_info(key, number):
    bus_info_list = []
    
    try:
        params = {'serviceKey': key, 'keyword': number, 'format': 'xml'}

        list_api_res = requests.get('http://apis.data.go.kr/6410000/busrouteservice/v2/getBusRouteListv2', params = params, timeout = 5)
        if not (list_api_res.headers.get('Content-Type').startswith('text/xml') or list_api_res.headers.get('Content-Type').startswith('application/xml')):
            return []
        
        list_api_tree = elemtree.fromstring(list_api_res.text)

        api_common_err = list_api_tree.find('./cmmMsgHeader/returnAuthMsg')
        if api_common_err != None:
            raise GyeonggiApiKeyError(api_common_err.text)
        
        api_err = int(list_api_tree.find('./msgHeader/resultCode').text)
        
        if api_err != 0 and api_err != 4:
            raise ValueError(list_api_tree.find('./msgHeader/resultMessage').text)
        
        if api_err != 4:
            list_api_body = list_api_tree.find('./msgBody')
            xml_bus_list = list_api_body.findall('./busRouteList')

            for i in xml_bus_list:
                name = i.find('./routeName').text
                route_id = i.find('./routeId').text
                region = i.find('./regionName').text
                route_type = int(i.find('./routeTypeCd').text)
                
                bus_info_list.append({'name': name, 'id': route_id, 'desc': region, 'type': route_type})
    except requests.exceptions.ConnectTimeout:
        print('Request Timeout')
    
    return bus_info_list

def search_busan_bus_info(key, number):
    bus_info_list = []
    params = {'serviceKey': key, 'lineno': number}
    
    success = False
    
    for i in range(20):
        try:
            list_api_res = requests.get('http://apis.data.go.kr/6260000/BusanBIMS/busInfo', params = params).text
            if list_api_res.find('http://apis.data.go.kr/503.html') != -1:
                raise ServerError('503 Server Unavailable')
                
            list_api_tree = elemtree.fromstring(list_api_res)
            
            api_common_err = list_api_tree.find('./cmmMsgHeader/returnAuthMsg')
            if api_common_err != None:
                raise BusanApiKeyError(api_common_err.text)
            
            api_err = int(list_api_tree.find('./header/resultCode').text)
            
            if api_err != 0:
                raise ValueError(list_api_tree.find('./header/resultMsg').text)
            
            xml_bus_list = list_api_tree.findall('./body/items/item')
            
            for i in xml_bus_list:
                name = i.find('./buslinenum').text
                route_id = i.find('./lineid').text
                start = i.find('./startpoint').text
                end = i.find('./endpoint').text
                route_type = convert_busan_bus_type(i.find('./bustype').text)
                
                bus_info_list.append({'name': name, 'id': route_id, 'desc': start + '~' + end, 'type': route_type})
        except Exception as e:
            error = e
            continue
        else:
            success = True
            break
    
    if not success:
        raise error
    
    return bus_info_list

# [신규] TAGO API로 버스 정보 검색 (특정 도시 코드 필요)
def search_tago_bus_info(key, number, cityCode, cityName):
    bus_info_list = []
    page_no = 1
    num_of_rows = 1000  # 한 페이지당 최대 행 수
    
    while True:
        try:
            # TAGO API는 'routeNo' (노선번호)로 검색
            params = {
                'serviceKey': key, 
                'routeNo': number, 
                'cityCode': cityCode,
                'pageNo': page_no,
                'numOfRows': num_of_rows,
                '_type': 'xml'
            }

            list_api_res = requests.get(
                'https://apis.data.go.kr/1613000/BusRouteInfoInqireService/getRouteNoList', 
                params=params, 
                timeout=10
            )
            
            if not (list_api_res.headers.get('Content-Type').startswith('text/xml') or 
                    list_api_res.headers.get('Content-Type').startswith('application/xml')):
                break
            
            list_api_tree = elemtree.fromstring(list_api_res.text)

            api_err_code = list_api_tree.find('./header/resultCode')
            if api_err_code is None:
                raise TagoApiKeyError("TAGO API 응답 형식이 올바르지 않습니다.")
            
            if api_err_code.text == '03':  # SERVICE_KEY_IS_NOT_REGISTERED_ERROR
                raise TagoApiKeyError(list_api_tree.find('./header/resultMsg').text)
            
            # 04: NODATA_ERROR (결과 없음), 00: 정상
            if api_err_code.text != '00' and api_err_code.text != '04':
                raise ValueError(list_api_tree.find('./header/resultMsg').text)
            
            if api_err_code.text == '04':  # 결과 없음
                break
            
            list_api_body = list_api_tree.find('./body/items')
            if list_api_body is None:
                break
                
            xml_bus_list = list_api_body.findall('./item')
            
            if not xml_bus_list:
                break

            for i in xml_bus_list:
                routeno_elem = i.find('./routeno')
                routeid_elem = i.find('./routeid')
                startnodenm_elem = i.find('./startnodenm')
                endnodenm_elem = i.find('./endnodenm')
                routetp_elem = i.find('./routetp')
                
                name = routeno_elem.text if routeno_elem is not None else None
                route_id = routeid_elem.text if routeid_elem is not None else None
                start = startnodenm_elem.text if startnodenm_elem is not None else '?'
                end = endnodenm_elem.text if endnodenm_elem is not None else '?'
                route_type = convert_tago_bus_type(routetp_elem.text) if routetp_elem is not None else 0
                
                # 'id'에 cityCode를 포함시켜, get_tago_... 함수들이 사용할 수 있게 함
                tago_route_id = f"TAGO|{cityCode}|{route_id}"
                
                if not name or not route_id:
                    continue
                    
                bus_info_list.append({
                    'name': name, 
                    'id': tago_route_id, 
                    'desc': f"{start}~{end}", 
                    'type': route_type
                })
            
            # 페이지당 행 수보다 적게 반환되면 마지막 페이지
            if len(xml_bus_list) < num_of_rows:
                break
                
            page_no += 1
                
        except requests.exceptions.ConnectTimeout:
            print(f'Request Timeout (TAGO {cityName}, 페이지 {page_no})')
            break
        except requests.exceptions.Timeout:
            print(f'Request Timeout (TAGO {cityName}, 페이지 {page_no})')
            break
        except Exception as e:
            print(f'TAGO {cityName} 검색 오류 (페이지 {page_no}): {str(e)}')
            break
    
    return bus_info_list

# [신규] TAGO API로 전체 도시 코드 목록 조회
def get_tago_city_codes(key):
    city_codes_list = []
    # endPoint Pasing (getCtyCodeList)
    params = {'serviceKey': key, '_type': 'xml'}
    
    try:
        list_api_res = requests.get('https://apis.data.go.kr/1613000/BusRouteInfoInqireService/getCtyCodeList', params = params, timeout = 10)
        
        if not (list_api_res.headers.get('Content-Type').startswith('text/xml') or list_api_res.headers.get('Content-Type').startswith('application/xml')):
            print("TAGO 도시 코드 조회 실패: XML 응답이 아닙니다.")
            return []

        list_api_tree = elemtree.fromstring(list_api_res.text)

        api_err_code = list_api_tree.find('./header/resultCode')
        if api_err_code is None:
            raise TagoApiKeyError("TAGO API 응답 형식이 올바르지 않습니다. (도시 코드 조회)")
        
        if api_err_code.text == '03': # SERVICE_KEY_IS_NOT_REGISTERED_ERROR
            raise TagoApiKeyError(list_api_tree.find('./header/resultMsg').text)
        
        if api_err_code.text != '00': # 00: 정상
            raise ValueError(list_api_tree.find('./header/resultMsg').text)
        
        list_api_body = list_api_tree.find('./body/items')
        if list_api_body is None:
            return []
            
        xml_city_list = list_api_body.findall('./item')

        for i in xml_city_list:
            city_code = i.find('./citycode').text
            city_name = i.find('./cityname').text
            city_codes_list.append({'name': city_name, 'code': city_code})
            
    except requests.exceptions.RequestException as e:
        print(f"TAGO 도시 코드 조회 중 오류 발생: {e}")
        return [] # 오류 발생 시 빈 리스트 반환
        
    return city_codes_list

_city_code_cache = {}

def get_city_name_from_code(city_code):
    """도시 코드를 도시 이름으로 변환 (캐시 사용)"""
    # 캐시에 있으면 반환
    if city_code in _city_code_cache:
        return _city_code_cache[city_code]
    
    # TAGO API 실제 도시 코드 매핑
    city_code_map = {
        '12': '세종',
        '22': '대구',
        '23': '인천',
        '24': '광주',
        '25': '대전',
        '26': '울산',
        '39': '제주',
        # 강원도
        '32010': '춘천',
        '32020': '원주',
        '32050': '태백',
        '32310': '홍천',
        '32360': '철원',
        '32410': '양양',
        # 충북
        '33010': '청주',
        '33020': '충주',
        '33030': '제천',
        '33320': '보은',
        '33330': '옥천',
        '33340': '영동',
        '33350': '진천',
        '33360': '괴산',
        '33370': '음성',
        '33380': '단양',
        # 충남
        '34010': '천안',
        '34020': '공주',
        '34040': '아산',
        '34050': '서산',
        '34060': '논산',
        '34070': '계룡',
        '34330': '부여',
        '34390': '당진',
        # 전북
        '35010': '전주',
        '35020': '군산',
        '35040': '정읍',
        '35050': '남원',
        '35060': '김제',
        '35320': '진안',
        '35330': '무주',
        '35340': '장수',
        '35350': '임실',
        '35360': '순창',
        '35370': '고창',
        '35380': '부안',
        # 전남
        '36010': '목포',
        '36020': '여수',
        '36030': '순천',
        '36040': '나주',
        '36060': '광양',
        '36320': '곡성',
        '36330': '구례',
        '36350': '고흥',
        '36380': '장흥',
        '36400': '해남',
        '36410': '영암',
        '36420': '무안',
        '36430': '함평',
        '36450': '장성',
        '36460': '완도',
        '36470': '진도',
        '36480': '신안',
        # 경북
        '37010': '포항',
        '37020': '경주',
        '37030': '김천',
        '37040': '안동',
        '37050': '구미',
        '37060': '영주',
        '37070': '영천',
        '37080': '상주',
        '37090': '문경',
        '37100': '경산',
        '37320': '의성',
        '37330': '청송',
        '37340': '영양',
        '37350': '영덕',
        '37360': '청도',
        '37370': '고령',
        '37380': '성주',
        '37390': '칠곡',
        '37400': '예천',
        '37410': '봉화',
        '37420': '울진',
        '37430': '울릉',
        # 경남
        '38010': '창원',
        '38030': '진주',
        '38050': '통영',
        '38060': '사천',
        '38070': '김해',
        '38080': '밀양',
        '38090': '거제',
        '38100': '양산',
        '38310': '의령',
        '38320': '함안',
        '38330': '창녕',
        '38340': '고성',
        '38350': '남해',
        '38360': '하동',
        '38370': '산청',
        '38380': '함양',
        '38390': '거창',
        '38400': '합천',
    }
    
    city_name = city_code_map.get(city_code, None)
    
    # 캐시에 저장
    if city_name:
        _city_code_cache[city_code] = city_name
    
    return city_name

def search_bus_info(key, number, return_error=False):
    bus_info_list = []
    exception = None
    
    # 1. 서울 버스 조회
    try:
        bus_info_list += search_seoul_bus_info(key, number)
    except ApiKeyError as api_err:
        exception = api_err
    except Exception as e:
        exception = ValueError('서울 버스 정보를 조회하는 중 오류가 발생했습니다: ' + str(e))
    
    # 2. 경기 버스 조회
    try:
        bus_info_list += search_gyeonggi_bus_info(key, number)
    except ApiKeyError as api_err:
        exception = api_err
    except Exception as e:
        exception = ValueError('경기 버스 정보를 조회하는 중 오류가 발생했습니다: ' + str(e))
    
    # 3. 부산 버스 조회
    try:
        bus_info_list += search_busan_bus_info(key, number)
    except ApiKeyError as api_err:
        exception = api_err
    except Exception as e:
        exception = ValueError('부산 버스 정보를 조회하는 중 오류가 발생했습니다: ' + str(e))
        
    # 4. TAGO API로 나머지 도시 병렬 검색
    try:
        all_tago_cities = get_tago_city_codes(key)
        
        # 서울, 경기, 부산 제외 + 경기도 개별 시군 제외
        excluded_cities = ['서울특별시', '경기도', '부산광역시']
        
        tago_cities_to_search = []
        for city in all_tago_cities:
            # 제외 목록에 있거나, 경기도 개별 시군(31xxx) 제외
            if city['name'] in excluded_cities:
                continue
            if city['code'].startswith('31'):  # 경기도 개별 시군 제외
                continue
            if city['code'] == '21':  # 부산 제외
                continue
            tago_cities_to_search.append((city['name'], city['code']))
        
        # 병렬 처리로 검색 속도 개선
        def search_single_city(city_info):
            city_name, city_code = city_info
            try:
                return search_tago_bus_info(key, number, city_code, city_name)
            except ApiKeyError:
                return None  # API 키 오류는 특별 처리
            except:
                return []  # 기타 오류는 빈 리스트

        # ThreadPoolExecutor로 병렬 검색 (최대 15개 동시 실행)
        with ThreadPoolExecutor(max_workers=15) as executor:
            # 모든 도시 검색 작업 제출
            future_to_city = {
                executor.submit(search_single_city, city_info): city_info 
                for city_info in tago_cities_to_search
            }
            
            # 결과가 나올 때마다 처리
            for future in as_completed(future_to_city):
                result = future.result()
                
                if result is None:  # API 키 오류
                    exception = TagoApiKeyError("TAGO API 키 오류")
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                elif result:  # 결과가 있으면
                    bus_info_list += result
                        
    except ApiKeyError as api_err:
        exception = api_err
    except Exception as e:
        exception = ValueError(f'TAGO 도시 코드 목록 조회 중 오류가 발생했습니다: {str(e)}')
    
    # 정렬 함수
    def search_sort_key(x):
        # 지역명 추출
        region = convert_type_to_region(x['type'], x.get('id'))
        
        # 노선번호 일치도 점수
        score = search_score(x, number)
        
        # (점수, 지역명) 튜플로 정렬
        # 점수가 낮을수록 우선, 같은 점수면 지역명 가나다순
        return (score, region if region else 'zzz')
    
    if return_error:
        return sorted(bus_info_list, key=search_sort_key), exception
    else:
        return sorted(bus_info_list, key=search_sort_key)

# search_score를 별도 함수로 분리
def search_score(x, number):
    rx_number = re.compile('[0-9]+')
    is_number = bool(re.match('[0-9]+$', number))
    
    if is_number:
        match = rx_number.search(x['name'])
        if match:
            main_x = match[0]
        else:
            main_x = x['name']
        
        score = match.start(0) if match else -1
        
        if x['name'] == number:
            return 0
        elif main_x == number:
            return 1
        
        if score == -1:
            return 0x7FFFFFFF
        else:
            return score * 10000 + (len(main_x) - len(number)) * 100
    else:
        if x['name'] == number:
            return ''
        else:
            return x['name']
    

# ... (get_naver_map, get_mapbox_map 함수는 변경 없음) ...
def get_naver_map(mapframe, naver_key_id, naver_key):
    route_size = mapframe.size()
    route_size_max = max(route_size)
    pos = mapframe.center()
    level = 12
    
    while 2 ** (21 - level) > route_size_max and level < 14:
        level += 1
    
    img_size = 2 ** (22 - level)
    k = img_size / 2.1
    
    if route_size[0] / img_size > 0.85 and route_size[1] / img_size > 0.85:
        map_part = [(1, -1), (-1, -1), (1, 1), (-1, 1)]
    elif route_size[0] / img_size > 0.85:
        map_part = [(1, 0), (-1, 0)]
    elif route_size[1] / img_size > 0.85:
        map_part = [(0, -1), (0, 1)]
    else:
        map_part = [(0, 0)]
        
    map_img = []
    
    for p in map_part:
        gps_pos = convert_gps((pos[0] + k * p[0], pos[1] + k * p[1]))
        map_img.append(requests.get('https://naveropenapi.apigw.ntruss.com/map-static/v2/raster?w=1024&h=1024&center={},{}&level={}&format=png&scale=2'.format(gps_pos[0], gps_pos[1], level), 
            headers={'X-NCP-APIGW-API-KEY-ID': naver_key_id, 'X-NCP-APIGW-API-KEY': naver_key}).content)
    
    result = ''
    
    for i in range(len(map_part)):
        result += '<image width="{0}" height="{0}" x="{1}" y="{2}" href="data:image/png;charset=utf-8;base64,{3}" />\n'.format(img_size, pos[0] + k * map_part[i][0] - img_size / 2, pos[1] + k * map_part[i][1] - img_size / 2, base64.b64encode(map_img[i]).decode('utf-8'))
    
    return result

def get_mapbox_map(mapframe, mapbox_key, mapbox_style, zoom_level=None):
    route_size_max = max(mapframe.size())
    
    if zoom_level is None:
        # 자동 계산
        level = 11
        while 2 ** (22 - level) > route_size_max and level < 14:
            level += 1
        level = max(11, min(14, level))
    else:
        # 사용자 지정 줌 레벨 사용 (11~14 범위로 제한)
        level = max(11, min(14, zoom_level))
    
    tile_size = 2 ** (21 - level)
    
    gps_pos = convert_gps((mapframe.left, mapframe.top))
    tile_x1, tile_y1 = mapbox.deg2num(gps_pos[1], gps_pos[0], level)
    
    gps_pos = convert_gps((mapframe.right, mapframe.bottom))
    tile_x2, tile_y2 = mapbox.deg2num(gps_pos[1], gps_pos[0], level)
    
    result = '<g id="background-map">\n'
    
    tile_pos = mapbox.num2deg(tile_x1, tile_y1, level)
    pos_x1, pos_y1 = convert_pos((tile_pos[1], tile_pos[0]))
    
    style_cache_dir = cache_dir + '/' + mapbox_style.replace("/", "_")
    if not os.path.exists(style_cache_dir):
        os.makedirs(style_cache_dir)
    
    for x in range(tile_x1, tile_x2 + 1):
        for y in range(tile_y1, tile_y2 + 1):
            cache_filename = style_cache_dir + '/tile{}-{}-z{}.svg'.format(x, y, level)
            cache_valid = False
            tile = None
            rx_svg = re.compile(r'<svg\s.*?>(.*)</svg>', flags = re.DOTALL)
            
            pos_x = pos_x1 + (x - tile_x1) * tile_size
            pos_y = pos_y1 + (y - tile_y1) * tile_size

            if os.path.exists(cache_filename):
                with open(cache_filename, mode='r', encoding='utf-8') as f:
                    text = f.read()
                    svg_match = rx_svg.search(text)
                    
                    if svg_match:
                        cache_valid = True
                        tile = svg_match[1]
            
            if not cache_valid:
                try:
                    cache_io = io.StringIO()
                    mapbox.load_tile(mapbox_style, mapbox_key, x, y, level, draw_full_svg = True, clip_mask = True, fp = cache_io)
                    
                    text = cache_io.getvalue()
                    tile = rx_svg.search(text)[1]

                    with open(cache_filename, mode='w+', encoding='utf-8') as cache_file:
                        cache_file.write(text)
                except:
                    os.remove(cache_filename)
                    raise
            
            result += '<g id="tile{0}-{1}-z{2}" transform="translate({3}, {4}) scale({5}, {5}) ">\n'.format(x, y, level, pos_x, pos_y, tile_size / 4096)
            result += tile
            result += '</g>\n'
            
    result += '</g>\n'
    
    return result
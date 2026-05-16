import streamlit as st
import math
import requests
from datetime import datetime, timedelta
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import folium
from folium import plugins
from folium.plugins import FloatImage
from streamlit_folium import st_folium
import pandas as pd
import io

# ==========================================
# ฟังก์ชันดึงราคาน้ำมัน Real-time
# ==========================================
@st.cache_data(ttl=21600) 
def fetch_today_oil_price():
    try:
        url = "https://api.chnwt.dev/thai-oil-api/latest"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            ptt_prices = data['response']['stations']['ptt']
            date_str = data['response']['date']
            
            target_types = ["ดีเซล", "แก๊สโซฮอล์ 91", "แก๊สโซฮอล์ 95"]
            oil_options = {}
            
            for key, val in ptt_prices.items():
                name = val['name']
                if any(target in name for target in target_types):
                    if "พรีเมียม" not in name and val['price'] and val['price'] != "-":
                        oil_options[name] = float(val['price'])
            return oil_options, date_str
    except Exception:
        pass
    return None, None

# ==========================================
# 1. ตั้งค่าหน้าเพจ UI
# ==========================================
st.set_page_config(page_title="Milk Run Optimization", page_icon="🚚", layout="wide")
st.title("🚚 ระบบวางแผนเส้นทางขนส่งนม (VRP Weight-Based Optimization)")
st.markdown("ระบบวิเคราะห์เส้นทางคำนวณจากน้ำหนักจริงรวมบรรจุภัณฑ์ พร้อมการนำทางจริงผ่าน TomTom API")

# ==========================================
# 2. แผงควบคุมด้านข้าง (Sidebar)
# ==========================================
with st.sidebar:
    st.header("🔑 การเข้าถึงระบบ")
    API_KEY = st.text_input("TomTom API Key", value="X8xbhfCgq1Tp192jy5KinmhP8wguznSu", type="password")
    
    st.header("⏱️ การปฏิบัติงาน")
    DEPART_TIME = st.time_input("เวลาเริ่มออกรถจากฟาร์ม", datetime.strptime("11:00", "%H:%M").time())
    SERVICE_TIME_SEC = st.number_input("เวลาลงนมเฉลี่ยต่อจุด (วินาที)", min_value=0, value=45, step=5)
    
    st.header("⛽ ราคาน้ำมัน")
    oil_data, update_date = fetch_today_oil_price()
    if oil_data:
        st.success(f"อัปเดตราคาล่าสุด: {update_date}")
        oil_list = list(oil_data.keys())
        default_oil_idx = 0
        for i, name in enumerate(oil_list):
            if "ดีเซล" in name:
                default_oil_idx = i
                break
        selected_oil = st.selectbox("เลือกชนิดน้ำมัน", oil_list, index=default_oil_idx)
        THB_L = st.number_input("ราคาน้ำมัน (THB/L)", value=float(oil_data[selected_oil]), step=0.5, format="%.2f")
    else:
        st.warning("⚠️ ไม่สามารถดึงข้อมูลราคา Real-time ได้")
        THB_L = st.number_input("ราคาน้ำมัน (THB/L)", min_value=1.0, value=35.0, step=0.5, format="%.2f")
    
    st.header("🚚 ข้อมูลยานพาหนะ")
    st.markdown("**ระบุจำนวนรถแต่ละประเภทที่มีพร้อมใช้งาน**")
    col1, col2 = st.columns(2)
    with col1:
        num_pickup = st.number_input("รถกระบะ (คัน)", min_value=0, value=0, step=1)
        num_4w = st.number_input("บรรทุก 4 ล้อ (คัน)", min_value=0, value=0, step=1)
    with col2:
        num_box = st.number_input("กระบะตู้ทึบ (คัน)", min_value=0, value=1, step=1)
        num_6w = st.number_input("บรรทุก 6 ล้อ (คัน)", min_value=0, value=0, step=1)

    st.markdown("**⛽ อัตราสิ้นเปลืองน้ำมันขณะวิ่ง (km/L)**")
    col3, col4 = st.columns(2)
    with col3:
        km_l_pickup = st.number_input("รถกระบะ", min_value=1.0, value=12.0, step=0.5, key="km_p")
        km_l_4w = st.number_input("บรรทุก 4 ล้อ", min_value=1.0, value=8.0, step=0.5, key="km_4")
    with col4:
        km_l_box = st.number_input("กระบะตู้ทึบ", min_value=1.0, value=10.0, step=0.5, key="km_b")
        km_l_6w = st.number_input("บรรทุก 6 ล้อ", min_value=1.0, value=6.0, step=0.5, key="km_6")

    st.markdown("**🛑 อัตราสิ้นเปลืองขณะจอดรถติด (L/h)**")
    col5, col6 = st.columns(2)
    with col5:
        idle_pickup = st.number_input("รถกระบะ", min_value=0.1, value=1.2, step=0.1, key="id_p")
        idle_4w = st.number_input("บรรทุก 4 ล้อ", min_value=0.1, value=2.0, step=0.1, key="id_4")
    with col6:
        idle_box = st.number_input("กระบะตู้ทึบ", min_value=0.1, value=1.5, step=0.1, key="id_b")
        idle_6w = st.number_input("บรรทุก 6 ล้อ", min_value=0.1, value=2.5, step=0.1, key="id_6")

    active_vehicles = []
    for _ in range(num_pickup): active_vehicles.append({'type': 'รถกระบะ', 'km_l': km_l_pickup, 'idle': idle_pickup})
    for _ in range(num_box): active_vehicles.append({'type': 'กระบะตู้ทึบ', 'km_l': km_l_box, 'idle': idle_box})
    for _ in range(num_4w): active_vehicles.append({'type': 'บรรทุก 4 ล้อ', 'km_l': km_l_4w, 'idle': idle_4w})
    for _ in range(num_6w): active_vehicles.append({'type': 'บรรทุก 6 ล้อ', 'km_l': km_l_6w, 'idle': idle_6w})

    # ✨ ปรับจากพิกัดปริมาตรถังนม เป็นน้ำหนักบรรทุกเพลาสูงสุดของรถจริง (กิโลกรัม)
    st.header("⚖️ ขีดจำกัดน้ำหนักบรรทุก")
    MAX_WEIGHT_CAPACITY = st.number_input("น้ำหนักบรรทุกสูงสุดต่อคัน (kg)", min_value=100, value=1100, step=50)
    DEAD_SPACE_RATIO = 0.15 
    
    st.header("🚧 ข้อจำกัดเส้นทาง")
    travel_mode_options = {
        "🚗 รถยนต์ (Car)": "car",
        "🚐 รถตู้ (Van)": "van",
        "🚚 รถบรรทุก (Truck)": "truck",
        "🏍️ รถจักรยานยนต์ (Motorcycle)": "motorcycle"
    }
    selected_mode_display = st.selectbox("ประเภทนำทาง", list(travel_mode_options.keys()), index=0)
    TRAVEL_MODE = travel_mode_options[selected_mode_display] 
    
    AVOID_AREA = st.text_area("พิกัดพื้นที่ห้ามผ่าน (ขึ้นบรรทัดใหม่สำหรับกล่องถัดไป)", value="", height=100)

EMISSION_FACTOR = 2.70757206 

# ==========================================
# 3. จัดการข้อมูล
# ==========================================
st.subheader("📍 นำเข้าข้อมูลจุดจัดส่ง")
uploaded_file = st.file_uploader("📂 อัปโหลดไฟล์รายการจัดส่ง (Excel หรือ CSV)", type=["csv", "xlsx"])

if uploaded_file is not None:
    try:
        df = pd.read_csv(uploaded_file) if uploaded_file.name.endswith('.csv') else pd.read_excel(uploaded_file)
        edited_df = st.data_editor(df, num_rows="dynamic", height=250, use_container_width=True)
    except Exception as e:
        st.error(f"❌ ไม่สามารถอ่านไฟล์ได้: {e}")
        st.stop()
else:
    st.info("💡 กรุณาอัปโหลดไฟล์ข้อมูลลูกค้าเพื่อเริ่มการวิเคราะห์")
    st.stop()

def time_to_min(t_str):
    try:
        h, m = map(int, str(t_str).split(':'))
        return h * 60 + m
    except: return None 

def haversine_distance(coord1, coord2):
    lat1, lon1 = coord1; lat2, lon2 = coord2
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin(math.radians(lat2 - lat1) / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2.0) ** 2
    return int(R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))))

# ==========================================
# 4. ประมวลผล (Optimization Core)
# ==========================================
st.markdown("---")
if st.button("🚀 ประมวลผลเส้นทางและวิเคราะห์เปรียบเทียบ", type="primary", use_container_width=True):
    
    total_vehicles = len(active_vehicles)
    if total_vehicles == 0:
        st.error("❌ กรุณาระบุจำนวนรถที่พร้อมใช้งานอย่างน้อย 1 คัน")
        st.stop()

    # ✨ ปรับตรรกะการคำนวณ Demand เป็นน้ำหนักจริงรวมบรรจุภัณฑ์ (กิโลกรัม)
    demands = []
    for i, row in edited_df.iterrows():
        if i == 0: demands.append(0); continue
        
        # น้ำหนักน้ำนม (ลิตร * 1.03) + น้ำหนักขวดเปล่าพลาสติก HDPE
        w_200cc = float(row.get("200cc", 0)) * 0.221  # นม 0.206 kg + ขวด 0.015 kg
        w_2l = float(row.get("2L", 0)) * 2.12        # นม 2.060 kg + ขวด 0.060 kg
        w_5l = float(row.get("5L", 0)) * 5.28        # นม 5.150 kg + ขวด 0.130 kg
        
        total_weight_kg = w_200cc + w_2l + w_5l
        demands.append(math.ceil(total_weight_kg * (1.0 + DEAD_SPACE_RATIO)))
    
    if sum(demands) > (MAX_WEIGHT_CAPACITY * total_vehicles):
        st.error(f"❌ น้ำหนักรวม ({sum(demands)} kg) เกินความจุของรถทั้งหมดรวมกัน ({MAX_WEIGHT_CAPACITY * total_vehicles} kg)")
        st.stop()
        
    with st.spinner(f'กำลังใช้สมองกลคำนวณเส้นทางจำกัดน้ำหนักสำหรับรถ {total_vehicles} คัน...'):
        coords = edited_df[['Lat', 'Lon']].values.tolist()
        dist_matrix = [[haversine_distance(coords[i], coords[j]) for j in range(len(coords))] for i in range(len(coords))]
        
        manager = pywrapcp.RoutingIndexManager(len(coords), total_vehicles, 0)
        routing = pywrapcp.RoutingModel(manager)
        
        def time_callback(from_index, to_index):
            d = dist_matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]
            return int((d / 1000) / 30 * 60) + (math.ceil(SERVICE_TIME_SEC / 60) if from_index != 0 else 0)
        
        transit_idx = routing.RegisterTransitCallback(time_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
        
        routing.AddDimension(transit_idx, 2880, 2880, False, "Time")
        time_dim = routing.GetDimensionOrDie("Time")
        
        for v in range(total_vehicles):
            time_dim.CumulVar(routing.Start(v)).SetValue(DEPART_TIME.hour * 60 + DEPART_TIME.minute)
        
        for i, row in edited_df.iterrows():
            idx = manager.NodeToIndex(i)
            s = time_to_min(row.get("เริ่มรับได้")) or 0
            e = time_to_min(row.get("ต้องส่งก่อน")) or 2880
            time_dim.CumulVar(idx).SetRange(s, 2880)
            if i != 0 and e < 2880:
                time_dim.SetCumulVarSoftUpperBound(idx, e, 100)

        def demand_callback(idx): return demands[manager.IndexToNode(idx)]
        demand_idx = routing.RegisterUnaryTransitCallback(demand_callback)
        
        # ✨ ปรับขีดจำกัดตัวแปรความจุสัมภาระในสมองกลให้เป็นข้อจำกัดน้ำหนัก (กิโลกรัม)
        routing.AddDimensionWithVehicleCapacity(demand_idx, 0, [MAX_WEIGHT_CAPACITY] * total_vehicles, True, "Capacity")

        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.AUTOMATIC
        search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        search_params.time_limit.seconds = 5
        solution = routing.SolveWithParameters(search_params)

    if solution:
        all_routes = []
        for vehicle_id in range(total_vehicles):
            index = routing.Start(vehicle_id)
            route_indices = []
            while not routing.IsEnd(index):
                route_indices.append(manager.IndexToNode(index))
                index = solution.Value(routing.NextVar(index))
            route_indices.append(manager.IndexToNode(index)) 
            
            if len(route_indices) > 2:
                all_routes.append({
                    'v_id': vehicle_id, 
                    'v_info': active_vehicles[vehicle_id], 
                    'indices': route_indices
                })

        route_results = []
        map_colors = ['#2980B9', '#27AE60', '#8E44AD', '#E67E22', '#C0392B', '#D35400', '#16A085']
        api_params = {"key": API_KEY, "travelMode": TRAVEL_MODE}
        
        rectangles = []
        if AVOID_AREA.strip() != "":
            for line in AVOID_AREA.strip().split('\n'):
                try:
                    p1, p2 = line.split(':')
                    lat1, lon1 = map(float, p1.split(','))
                    lat2, lon2 = map(float, p2.split(','))
                    rectangles.append({
                        "southWestCorner": {"latitude": min(lat1, lat2), "longitude": min(lon1, lon2)},
                        "northEastCorner": {"latitude": max(lat1, lat2), "longitude": max(lon1, lon2)}
                    })
                except: pass
        
        total_dist_km, total_cost_thb, total_co2_kg, max_time_sec = 0, 0, 0, 0
        
        for idx, route in enumerate(all_routes):
            indices = route['indices']
            v_info = route['v_info']
            
            url = f"https://api.tomtom.com/routing/1/calculateRoute/{':'.join([f'{coords[n][0]},{coords[n][1]}' for n in indices])}/json"
            
            if rectangles:
                res = requests.post(url, params=api_params, json={"avoidAreas": {"rectangles": rectangles}})
            else:
                res = requests.get(url, params=api_params)
            
            if res.status_code == 200:
                data = res.json()['routes'][0]
                summary = data['summary']
                
                dist_km = summary['lengthInMeters'] / 1000
                travel_time = summary['travelTimeInSeconds']
                traffic_delay_sec = summary.get('trafficDelayInSeconds', 0)
                
                fuel_running = dist_km / v_info['km_l']
                fuel_idling = (traffic_delay_sec / 3600) * v_info['idle']
                
                total_fuel_l = fuel_running + fuel_idling
                cost_thb = total_fuel_l * THB_L
                co2_kg = total_fuel_l * EMISSION_FACTOR
                
                total_dist_km += dist_km
                total_cost_thb += cost_thb
                total_co2_kg += co2_kg
                max_time_sec = max(max_time_sec, travel_time)
                
                route_results.append({
                    'car_name': f"คันที่ {idx+1} ({v_info['type']})",
                    'data': data,
                    'indices': indices,
                    'color': map_colors[idx % len(map_colors)],
                    'v_info': v_info
                })
            else:
                st.error(f"❌ API Error สำหรับรถคันที่ {idx+1}: {res.text}")

        st.subheader(f"📊 การวิเคราะห์ผลลัพธ์รวม (ใช้งานรถทั้งหมด {len(route_results)} คัน)")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("ระยะทางรวม", f"{total_dist_km:.2f} กม.")
        c2.metric("ต้นทุนน้ำมันรวม", f"฿{total_cost_thb:.2f}")
        c3.metric("ปริมาณการปล่อย CO2", f"{total_co2_kg:.2f} kg")
        hh, mm = divmod(max_time_sec // 60, 60)
        c4.metric("เวลาวิ่งนานสุด (คันที่ช้าสุด)", f"{int(hh)} ชม. {int(mm)} นาที")

        col_map, col_table = st.columns([1.3, 1.7])
        with col_map:
            st.subheader("🗺️ แผนที่เส้นทางแยกรถแต่ละคัน")
            m = folium.Map(location=coords[0], zoom_start=14, control_scale=True)
            
            folium.TileLayer(
                tiles=f"https://api.tomtom.com/traffic/map/4/tile/flow/relative0-dark/{{z}}/{{x}}/{{y}}.png?key={API_KEY}",
                attr='TomTom Traffic', name='จราจร', overlay=True, control=True, opacity=0.7
            ).add_to(m)

            folium.Marker(coords[0], popup="ฟาร์ม", icon=folium.Icon(color='green', icon='home')).add_to(m)
            
            for rect in rectangles:
                sw, ne = rect['southWestCorner'], rect['northEastCorner']
                folium.Rectangle(
                    bounds=[[sw['latitude'], sw['longitude']], [ne['latitude'], ne['longitude']]],
                    color='#E74C3C', fill=True, fill_color='#E74C3C', fill_opacity=0.3
                ).add_to(m)

            for rr in route_results:
                all_points = []
                for leg in rr['data']['legs']:
                    for p in leg['points']: all_points.append([p['latitude'], p['longitude']])
                
                plugins.AntPath(
                    locations=all_points, delay=800, dash_array=[15, 30], 
                    color=rr['color'], pulse_color="#FFFFFF", weight=6, opacity=0.8,
                    name=f"เส้นทาง {rr['car_name']}"
                ).add_to(m)
                
                for step, n in enumerate(rr['indices'][1:-1]):
                    loc = edited_df.iloc[n]
                    icon_html = f'''<div style="font-size: 10pt; font-weight: bold; color: white; background-color: {rr['color']}; border: 2px solid white; border-radius: 50%; text-align: center; width: 24px; height: 24px; line-height: 20px;">{step+1}</div>'''
                    folium.Marker([loc['Lat'], loc['Lon']], popup=f"{rr['car_name']} | ลำดับ: {step+1}<br>{loc['ชื่อสถานที่']}", icon=folium.DivIcon(html=icon_html)).add_to(m)
            
            folium.LayerControl().add_to(m)
            st_folium(m, width="100%", height=500, returned_objects=[])

        with col_table:
            st.subheader("📋 ตารางวิเคราะห์คิวงานรวม")
            schedule = []
            
            for rr in route_results:
                curr_time = datetime.combine(datetime.today(), DEPART_TIME)
                v_info = rr['v_info']
                
                for i, n in enumerate(rr['indices'][:-1]):
                    t_min, l_dist, fuel_used = 0, 0.0, 0.0
                    loc_data = edited_df.iloc[n]
                    
                    if i > 0:
                        leg = rr['data']['legs'][i-1]['summary']
                        t_min = math.ceil(leg['travelTimeInSeconds'] / 60)
                        l_dist = leg['lengthInMeters'] / 1000
                        delay = leg.get('trafficDelayInSeconds', 0)
                        
                        f_run = l_dist / v_info['km_l']
                        f_idle = (delay / 3600) * v_info['idle']
                        fuel_used = f_run + f_idle
                        
                        curr_time += timedelta(minutes=t_min)
                    
                    maps_url = f"https://www.google.com/maps/dir/?api=1&destination={loc_data['Lat']},{loc_data['Lon']}"
                    
                    schedule.append({
                        "คันที่": rr['car_name'],
                        "สถานที่": loc_data["ชื่อสถานที่"] if i > 0 else "ออกเดินทางจากฟาร์ม", 
                        "ถึงเวลา": curr_time.strftime("%H:%M"),
                        "ระยะทาง(กม.)": f"{l_dist:.2f}" if i > 0 else "-",
                        "น้ำมัน(L)": f"{fuel_used:.2f}" if i > 0 else "-", 
                        "นำทาง": maps_url if i > 0 else None
                    })
                    curr_time += timedelta(seconds=SERVICE_TIME_SEC)
            
            df_schedule = pd.DataFrame(schedule)
            st.dataframe(
                df_schedule, use_container_width=True, hide_index=True,
                column_config={"นำทาง": st.column_config.LinkColumn("📍 นำทาง", display_text="เปิดแผนที่")}
            )
            
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                df_schedule.to_excel(writer, index=False, sheet_name='MilkRun_Plan')
            st.download_button("📥 ดาวน์โหลดใบงาน Excel", buf.getvalue(), "MilkRun_Detail_Plan.xlsx", use_container_width=True)

    else:
        st.error("❌ หาเส้นทางไม่ได้ (เงื่อนไขเวลาตึงเกินไป หรือน้ำหนักรวมเกินกำลังรถ)")

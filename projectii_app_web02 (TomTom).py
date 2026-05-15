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
# 1. ตั้งค่าหน้าเพจ UI
# ==========================================
st.set_page_config(page_title="Milk Run (TomTom Engine)", page_icon="📍", layout="wide")
st.title("📍 ระบบวางแผนเส้นทางขนส่งนม (TomTom API)")
st.markdown("วิเคราะห์เส้นทางด้วยสมองกล OR-Tools และแสดงผลเส้นทางแบบรวดเดียวจบ (รองรับสูงสุด 150 จุด) ด้วย TomTom")

# ==========================================
# 2. แผงควบคุมด้านข้าง (Sidebar)
# ==========================================
with st.sidebar:
    st.header("🔑 การเข้าถึงระบบ")
    API_KEY = st.text_input("TomTom API Key", value="", type="password")
    st.caption("แนะนำ: กรอกคีย์ TomTom ของคุณเพื่อเริ่มใช้งาน")
    
    st.header("⏱️ การปฏิบัติงาน")
    DEPART_TIME = st.time_input("เวลาเริ่มออกรถจากฟาร์ม", datetime.strptime("11:20", "%H:%M").time())
    SERVICE_TIME_SEC = st.number_input("เวลาลงนมเฉลี่ยต่อจุด (วินาที)", min_value=0, value=45, step=5)
    
    st.header("⛽ ต้นทุนและพื้นที่บรรทุก")
    THB_L = st.number_input("ราคาน้ำมัน (THB/L)", min_value=1.0, value=40.0, step=0.5, format="%.2f")
    KM_L = st.number_input("อัตราสิ้นเปลือง (km/L)", min_value=1.0, value=10.0, step=0.5, format="%.2f")
    NUM_COOLERS = st.number_input("จำนวนถัง (ใบ)", min_value=1, value=2, step=1)
    ICE_PER_COOLER = st.number_input("น้ำแข็ง/ถัง (L)", min_value=0.0, value=75.0, step=1.0)
    DEAD_SPACE_RATIO = 0.15 
    
    st.header("🚧 ข้อจำกัดเส้นทาง")
    TRAVEL_MODE = st.selectbox("ประเภทยานพาหนะ", ["car", "truck", "van"], index=0) 

TOTAL_NET_CAPACITY = int((450 - ICE_PER_COOLER) * NUM_COOLERS)
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

# ==========================================
# ฟังก์ชันคำนวณพื้นฐาน
# ==========================================
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

def safe_float(val):
    try:
        f = float(val)
        return 0.0 if math.isnan(f) else f
    except: return 0.0

# ==========================================
# 4. ประมวลผล (Optimization Core)
# ==========================================
st.markdown("---")
if st.button("🚀 ประมวลผลด้วย TomTom API", type="primary", use_container_width=True):
    if not API_KEY:
        st.error("❌ กรุณาใส่ API Key ในช่องด้านซ้ายมือก่อนรันระบบ")
        st.stop()

    demands = []
    for i, row in edited_df.iterrows():
        if i == 0: demands.append(0); continue
        vol = (safe_float(row.get("200cc", 0)) * 0.2) + (safe_float(row.get("2L", 0)) * 2.0) + (safe_float(row.get("5L", 0)) * 5.0)
        demands.append(math.ceil(vol * (1.0 + DEAD_SPACE_RATIO)))
    
    if sum(demands) > TOTAL_NET_CAPACITY:
        st.error(f"❌ น้ำหนักรวมเกินความจุรถ ({TOTAL_NET_CAPACITY} L)")
        st.stop()
        
    with st.spinner('กำลังให้ OR-Tools จัดลำดับคิวจัดส่ง...'):
        coords = edited_df[['Lat', 'Lon']].values.tolist()
        dist_matrix = [[haversine_distance(coords[i], coords[j]) for j in range(len(coords))] for i in range(len(coords))]
        baseline_km = sum([dist_matrix[i][i+1] for i in range(len(coords)-1)] + [dist_matrix[len(coords)-1][0]]) / 1000
        
        manager = pywrapcp.RoutingIndexManager(len(coords), 1, 0)
        routing = pywrapcp.RoutingModel(manager)
        
        def time_callback(from_index, to_index):
            d = dist_matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]
            return int((d / 1000) / 30 * 60) + (math.ceil(SERVICE_TIME_SEC / 60) if from_index != 0 else 0)
        
        transit_idx = routing.RegisterTransitCallback(time_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
        
        routing.AddDimension(transit_idx, 2880, 2880, False, "Time")
        time_dim = routing.GetDimensionOrDie("Time")
        time_dim.CumulVar(routing.Start(0)).SetValue(DEPART_TIME.hour * 60 + DEPART_TIME.minute)
        
        for i, row in edited_df.iterrows():
            idx = manager.NodeToIndex(i)
            s = time_to_min(row.get("เริ่มรับได้")) or 0
            e = time_to_min(row.get("ต้องส่งก่อน")) or 2880
            time_dim.CumulVar(idx).SetRange(s, 2880)
            if i != 0 and e < 2880:
                time_dim.SetCumulVarSoftUpperBound(idx, e, 100)

        def demand_callback(idx): return demands[manager.IndexToNode(idx)]
        demand_idx = routing.RegisterUnaryTransitCallback(demand_callback)
        routing.AddDimensionWithVehicleCapacity(demand_idx, 0, [TOTAL_NET_CAPACITY], True, "Capacity")

        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.AUTOMATIC
        search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        search_params.time_limit.seconds = 5
        solution = routing.SolveWithParameters(search_params)

    if solution:
        route_indices = []
        index = routing.Start(0)
        while not routing.IsEnd(index):
            route_indices.append(manager.IndexToNode(index))
            index = solution.Value(routing.NextVar(index))
        route_indices.append(0)

        # ----------------------------------------------------
        # การเรียก TomTom API (ยิงรวดเดียวจบ ไม่ต้อง Chunking)
        # ----------------------------------------------------
        with st.spinner('กำลังเชื่อมต่อ TomTom API เพื่อวาดเส้นทางจริง...'):
            api_success = False
            api_error_msg = ""
            all_points = []
            all_legs = []
            
            # TomTom ใช้รูปแบบพิกัดเป็น Lat,Lon:Lat,Lon:Lat,Lon
            locations_str = ":".join([f"{coords[n][0]},{coords[n][1]}" for n in route_indices])
            
            url = f"https://api.tomtom.com/routing/1/calculateRoute/{locations_str}/json"
            params = {
                "key": API_KEY,
                "routeType": "fastest",
                "traffic": "true",
                "travelMode": TRAVEL_MODE
            }
            
            res = requests.get(url, params=params)
            
            if res.status_code == 200:
                data = res.json()
                if 'routes' in data and len(data['routes']) > 0:
                    api_success = True
                    route_data = data['routes'][0]
                    all_legs = route_data.get('legs', [])
                    
                    # ดึงข้อมูลจุดพิกัดเส้นทางของ TomTom (ไม่ต้องใช้ Polyline Decoder)
                    for leg in all_legs:
                        for point in leg.get('points', []):
                            all_points.append([point['latitude'], point['longitude']])
                            
                    total_dist_meters = route_data['summary']['lengthInMeters']
                    total_time_seconds = route_data['summary']['travelTimeInSeconds']
                else:
                    api_error_msg = "TomTom API ไม่สามารถสร้างเส้นทางได้ (ข้อมูลตอบกลับว่างเปล่า)"
            else:
                api_error_msg = f"HTTP Error: {res.status_code} - {res.text}"

        if api_success:
            dist_km = total_dist_meters / 1000
            cost = (dist_km / KM_L) * THB_L
            dist_delta = dist_km - baseline_km
            
            # --- Dashboard ---
            st.subheader("📊 การวิเคราะห์ผลลัพธ์รวม (TomTom Engine)")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("ระยะทางรวม", f"{dist_km:.2f} กม.", f"{dist_delta:.2f} กม.", delta_color="inverse")
            c2.metric("ต้นทุนน้ำมัน", f"฿{cost:.2f}", f"฿{(dist_delta/KM_L)*THB_L:.2f}", delta_color="inverse")
            c3.metric("CO2 ทั้งเที่ยว", f"{(dist_km/KM_L)*EMISSION_FACTOR:.2f} kg", f"{(dist_delta/KM_L)*EMISSION_FACTOR:.2f} kg", delta_color="inverse")
            hh, mm = divmod(total_time_seconds // 60, 60)
            c4.metric("เวลาเดินทางรวม", f"{int(hh)} ชม. {int(mm)} นาที" if hh > 0 else f"{int(mm)} นาที")

            # --- แผนที่และตาราง ---
            col_map, col_table = st.columns([1.3, 1.7])
            with col_map:
                st.subheader("🗺️ แผนที่เส้นทาง (TomTom Layer)")
                
                m = folium.Map(location=coords[0], zoom_start=14, control_scale=True)
                north_arrow_url = "https://upload.wikimedia.org/wikipedia/commons/e/ec/Compass_rose_n_blank.svg"
                FloatImage(north_arrow_url, bottom=5, left=90, width="6%").add_to(m)
                
                # สีเส้น AntPath สีแดงส้ม (TomTom Color)
                plugins.AntPath(
                    locations=all_points,
                    delay=800, dash_array=[15, 30], color="#DF1B12", pulse_color="#FFFFFF", weight=6,
                    name='TomTom Route (AntPath)'
                ).add_to(m)
                
                for i, n in enumerate(route_indices[:-1]):
                    loc = edited_df.iloc[n]
                    if n == 0:
                        folium.Marker([loc['Lat'], loc['Lon']], popup="ฟาร์ม", icon=folium.Icon(color='red', icon='home')).add_to(m)
                    else:
                        icon_html = f'''<div style="font-size: 11pt; font-weight: bold; color: white; background-color: #333333; border: 2px solid white; border-radius: 50%; text-align: center; width: 28px; height: 28px; line-height: 24px;">{i}</div>'''
                        folium.Marker([loc['Lat'], loc['Lon']], popup=f"คิว {i}: {loc['ชื่อสถานที่']}", icon=folium.DivIcon(html=icon_html)).add_to(m)
                
                folium.LayerControl().add_to(m)
                st_folium(m, width="100%", height=500, returned_objects=[])
                
                map_html = io.BytesIO()
                m.save(map_html, close_file=False)
                st.download_button(
                    label="💾 ดาวน์โหลดแผนที่ (HTML)", data=map_html.getvalue(),
                    file_name="TomTom_Optimized_Route.html", mime="text/html", use_container_width=True
                )

            with col_table:
                st.subheader("📋 ตารางวิเคราะห์เส้นทางเชิงลึก (Speed Heuristics)")
                schedule = []
                curr_time = datetime.combine(datetime.today(), DEPART_TIME)
                
                for i, n in enumerate(route_indices[:-1]):
                    t_min, l_dist, f_used, c_leg, avg_speed = 0, 0.0, 0.0, 0.0, 0.0
                    road_type = "-"
                    loc_data = edited_df.iloc[n]
                    
                    if i > 0 and len(all_legs) >= i:
                        leg = all_legs[i-1] 
                        duration_sec = leg['summary']['travelTimeInSeconds']
                        l_dist_meters = leg['summary']['lengthInMeters']
                        
                        t_min = math.ceil(duration_sec / 60)
                        l_dist = l_dist_meters / 1000
                        f_used = l_dist / KM_L
                        c_leg = f_used * EMISSION_FACTOR
                        
                        # คำนวณความเร็วเฉลี่ยของช่วงเส้นทางนี้ (V = S/T)
                        if duration_sec > 0:
                            avg_speed = l_dist / (duration_sec / 3600)
                            
                            # วิเคราะห์ประเภทถนนตามความเร็วเฉลี่ย
                            if avg_speed >= 45:
                                road_type = "ถนนหลัก"
                            elif avg_speed >= 25:
                                road_type = "ถนนรอง"
                            else:
                                road_type = "ซอย/รถติด"
                            
                        curr_time += timedelta(minutes=t_min)
                    
                    # ลิงก์นำทาง (ยังคงใช้ Google Maps URL ได้เพื่อความสะดวกของคนขับ)
                    maps_url = f"https://www.google.com/maps/dir/?api=1&destination={loc_data['Lat']},{loc_data['Lon']}"
                    
                    schedule.append({
                        "คิว": i, 
                        "สถานที่": loc_data["ชื่อสถานที่"], 
                        "เวลาที่ถึง": curr_time.strftime("%H:%M"),
                        "นำทาง": maps_url if i > 0 else None, 
                        "ระยะทาง (กม.)": f"{l_dist:.2f}" if i > 0 else "-", 
                        "เวลา (นาที)": t_min if i > 0 else "-", 
                        "ลักษณะถนน": road_type,
                        "ความเร็วเฉลี่ย (km/h)": f"{avg_speed:.0f}" if i > 0 else "-",
                        "น้ำมัน (ลิตร)": f"{f_used:.2f}" if i > 0 else "-"
                    })
                    curr_time += timedelta(seconds=SERVICE_TIME_SEC)
                
                df_schedule = pd.DataFrame(schedule)
                st.dataframe(
                    df_schedule, use_container_width=True, hide_index=True,
                    column_config={"นำทาง": st.column_config.LinkColumn("📍 นำทาง", display_text="เปิดแผนที่")}
                )
                
                st.markdown("---")
                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                    df_schedule.to_excel(writer, index=False, sheet_name='MilkRun_Plan')
                st.download_button("📥 ดาวน์โหลดใบงาน Excel", buf.getvalue(), "TomTom_MilkRun_Plan.xlsx", use_container_width=True)
        else:
            st.error(f"❌ TomTom API Error: {api_error_msg}")
    else:
        st.error("❌ หาเส้นทางไม่ได้ (น้ำหนักเกินหรือเงื่อนไขเวลาขัดแย้งกัน)")

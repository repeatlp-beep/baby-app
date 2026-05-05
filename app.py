import streamlit as st
from streamlit_gsheets import GSheetsConnection
import pandas as pd
from streamlit_mic_recorder import speech_to_text
import re
import uuid
from datetime import datetime, timedelta


def _safe_float(val, default=0.0):
    if pd.isna(val):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _get_sheet_url():
    sheet_id = st.secrets["connections"]["gsheets"]["spreadsheet_id"]
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def normalize_voice_text(text):
    """STT 오인식을 보정해 파싱 정확도를 올립니다."""
    if not text:
        return text

    normalized = text.strip()

    # 단위 오인식 보정 (예: "120 mm", "120 미리" -> "120 ml")
    normalized = re.sub(r"(\d+)\s*(mm|MM|미리)\b", r"\1 ml", normalized)
    normalized = normalized.replace("밀리리터", "ml").replace("밀리", "ml")

    # 분유 유사 발음/오인식 보정
    for token in ["부뉴", "분요", "분유우", "붕유", "분유유", "분유로", "분유를"]:
        normalized = normalized.replace(token, "분유")

    return normalized


def _parse_minutes(text):
    hour_m = re.search(r"(\d+)\s*시간", text)
    min_m = re.search(r"(\d+)\s*분", text)
    return (int(hour_m.group(1)) * 60 if hour_m else 0) + (int(min_m.group(1)) if min_m else 0)


def append_records(df_new):
    """새 레코드를 기존 시트 상단에 추가 저장합니다."""
    full_url = _get_sheet_url()
    old_df = load_data()

    if old_df is None or old_df.empty:
        conn.update(spreadsheet=full_url, data=df_new)
        return

    missing = [c for c in old_df.columns if c not in df_new.columns]
    extra = [c for c in df_new.columns if c not in old_df.columns]
    if missing or extra:
        raise ValueError(f"컬럼 불일치: missing={missing}, extra={extra}")

    df_new = df_new[old_df.columns]
    merged = pd.concat([df_new, old_df], ignore_index=True)
    conn.update(spreadsheet=full_url, data=merged)


def save_updates_by_id(edited_df):
    """ID 기준으로 기존 시트 행을 업데이트합니다."""
    latest_df = load_data()
    if latest_df is None or latest_df.empty:
        raise ValueError("시트 데이터를 다시 불러오지 못했습니다.")

    edit_map = {str(row["ID"]): row for _, row in edited_df.iterrows()}
    merged_df = latest_df.copy()
    for idx, row in merged_df.iterrows():
        rid = str(row.get("ID", ""))
        if rid in edit_map:
            merged_df.loc[idx, edited_df.columns] = edit_map[rid].values

    conn.update(spreadsheet=_get_sheet_url(), data=merged_df)


# --- 1. 페이지 설정 및 커스텀 스타일 ---
st.set_page_config(page_title="윤겸이 육아 기록", page_icon="👶", layout="wide")

st.markdown("""
    <style>
    .stButton>button { width: 100%; border-radius: 20px; height: 3em; font-weight: bold; border: 1px solid #e0e0e0; }
    .record-card { padding: 15px; border-radius: 10px; border: 1px solid #f0f2f6; margin-bottom: 10px; }
    @media (max-width: 768px) {
        .stButton>button { height: 2.8em; font-size: 0.95rem; }
        [data-testid="column"] { width: 100% !important; flex: 1 1 100% !important; }
    }
    </style>
    """, unsafe_allow_html=True)

# --- 2. 구글 시트 연결 (최적화 반영) ---
conn = st.connection("gsheets", type=GSheetsConnection)

def load_data():
    """secrets.toml에 지정된 시트 ID에서 데이터를 불러옵니다."""
    sheet_id = st.secrets["connections"]["gsheets"]["spreadsheet_id"]
    # ttl="0s"로 설정하여 캐싱 없이 항상 최신 데이터를 불러옵니다.
    # 새로 추가된 줄: ID를 가지고 완전한 구글 시트 주소 만들기
    full_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    # NOTE: 일부 환경에서 한글 워크시트명(예: "기록")을 전달하면 내부에서 ASCII 인코딩을 시도하며
    # UnicodeEncodeError가 발생할 수 있어 worksheet 파라미터를 생략합니다(기본: 첫 워크시트).
    return conn.read(spreadsheet=full_url, ttl="0s")

# --- 3. STT 및 데이터 파싱 로직 ---
# def recognize_speech():
#     """마이크를 통해 음성을 인식하고 텍스트로 반환합니다."""
#     r = sr.Recognizer()
#     with sr.Microphone() as source:
#         st.toast("🎤 듣고 있습니다... 편하게 말씀해 주세요.")
#         r.adjust_for_ambient_noise(source, duration=0.5)
#         try:
#             audio = r.listen(source, timeout=5, phrase_time_limit=8)
#             return r.recognize_google(audio, language='ko-KR')
#         except sr.WaitTimeoutError:
#             return None
#         except sr.UnknownValueError:
#             return None
#         except sr.RequestError:
#             return None
#         except OSError:
#             return None



def parse_to_records(text, base_time):
    """음성 텍스트를 분석하여 구글 시트 구조에 맞는 레코드 리스트를 생성합니다."""
    text = normalize_voice_text(text)
    records = []
    # 데이터 구조 템플릿
    template = {
        "ID": "", 
        "시작시간": "", 
        "종료시간": "", 
        "분류1": "기타", 
        "분류2": "", 
        "분류3": "", 
        "양(ml)": 0, 
        "소요시간(분)": 0, 
        "원본 음성": text
    }

    # [수유 - 모유] 로직: "모유"가 있을 때만 분기 (왼쪽/오른쪽만 말한 오탐 방지)
    if "모유" in text and "분유" not in text and "유축" not in text:
        left_m = re.search(r'왼쪽.*?(\d+)\s*분', text)
        right_m = re.search(r'오른쪽.*?(\d+)\s*분', text)
        matches = []
        if left_m:
            matches.append({'side': '왼쪽', 'dur': int(left_m.group(1)), 'pos': left_m.start()})
        if right_m:
            matches.append({'side': '오른쪽', 'dur': int(right_m.group(1)), 'pos': right_m.start()})
        matches.sort(key=lambda x: x['pos'])
        if not matches:
            gm = re.search(r'모유.*?(\d+)\s*분', text) or re.search(r'(\d+)\s*분.*?모유', text)
            if gm:
                matches.append({'side': '', 'dur': int(gm.group(1)), 'pos': gm.start()})

        curr_start = base_time
        for m in matches:
            rec = template.copy()
            rec.update({
                "ID": str(uuid.uuid4())[:8],
                "분류1": "수유",
                "분류2": "모유",
                "분류3": m['side'],
                "소요시간(분)": m['dur'],
                "시작시간": curr_start.strftime("%Y-%m-%d %H:%M:%S")
            })
            end = curr_start + timedelta(minutes=m['dur'])
            rec["종료시간"] = end.strftime("%Y-%m-%d %H:%M:%S")
            records.append(rec)
            curr_start = end

    # [수유 - 분유/유축수유] 로직
    elif any(k in text for k in ["분유", "유축"]):
        amount = re.search(r'(\d+)\s*(ml|ML|Mm|MM|미리|밀리|밀리리터)', text)
        dur = re.search(r'(\d+)\s*분', text)
        rec = template.copy()
        rec.update({
            "ID": str(uuid.uuid4())[:8], 
            "분류1": "수유", 
            "분류2": "분유" if "분유" in text else "유축수유",
            "양(ml)": int(amount.group(1)) if amount else 0,
            "소요시간(분)": int(dur.group(1)) if dur else 0,
            "시작시간": base_time.strftime("%Y-%m-%d %H:%M:%S")
        })
        end = base_time + timedelta(minutes=rec["소요시간(분)"])
        rec["종료시간"] = end.strftime("%Y-%m-%d %H:%M:%S")
        records.append(rec)

    # [수면] 로직 ("자" 단독 부분 문자열 오탐 방지)
    elif "잠" in text or re.search(r"(낮잠|밤잠|재웠|취침)", text):
        total_m = _parse_minutes(text)
        rec = template.copy()
        rec.update({
            "ID": str(uuid.uuid4())[:8], 
            "분류1": "수면", 
            "분류2": "밤잠" if "밤" in text else "낮잠",
            "소요시간(분)": total_m, 
            "시작시간": base_time.strftime("%Y-%m-%d %H:%M:%S")
        })
        rec["종료시간"] = (base_time + timedelta(minutes=total_m)).strftime("%Y-%m-%d %H:%M:%S")
        records.append(rec)

    # [기저귀] 대변/소변 분류
    elif "기저귀" in text or "똥" in text or "응가" in text or re.search(r"(소변|오줌)", text):
        rec = template.copy()
        rec.update({
            "ID": str(uuid.uuid4())[:8], 
            "분류1": "기저귀", 
            "분류2": "대변" if any(k in text for k in ["대변", "똥", "응가"]) else "소변",
            "시작시간": base_time.strftime("%Y-%m-%d %H:%M:%S"),
            "종료시간": base_time.strftime("%Y-%m-%d %H:%M:%S") # 순간 기록
        })
        records.append(rec)

    # [목욕] 시간 기록
    elif any(k in text for k in ["목욕", "씻", "샤워"]):
        total_m = _parse_minutes(text)
        rec = template.copy()
        rec.update({
            "ID": str(uuid.uuid4())[:8],
            "분류1": "목욕",
            "분류2": "목욕",
            "소요시간(분)": total_m,
            "시작시간": base_time.strftime("%Y-%m-%d %H:%M:%S")
        })
        rec["종료시간"] = (base_time + timedelta(minutes=total_m)).strftime("%Y-%m-%d %H:%M:%S")
        records.append(rec)

    # [터미타임] 시간 기록
    elif any(k in text for k in ["터미타임", "터미", "배깔기", "엎드려"]):
        total_m = _parse_minutes(text)
        rec = template.copy()
        rec.update({
            "ID": str(uuid.uuid4())[:8],
            "분류1": "터미타임",
            "분류2": "터미타임",
            "소요시간(분)": total_m,
            "시작시간": base_time.strftime("%Y-%m-%d %H:%M:%S")
        })
        rec["종료시간"] = (base_time + timedelta(minutes=total_m)).strftime("%Y-%m-%d %H:%M:%S")
        records.append(rec)

    return records

# --- 4. 메인 화면 UI 레이아웃 ---
tab1, tab2 = st.tabs(["📋 활동 기록 및 관리", "📈 성장 및 활동 통계"])

with tab1:
    st.title("👶 윤겸이 육아 기록부")
    st.markdown("버튼을 누르고 활동을 말씀해 주세요. (예: `왼쪽 모유 10분, 오른쪽 15분 먹였어`)")
    
    # 🎙️ 음성 입력 버튼
    # col_mic, col_blank = st.columns([1, 1])
    # with col_mic:
    #     if st.button("🎙️ 음성 인식 시작"):
    #         voice_text = recognize_speech()
    #         if voice_text:
    #             normalized_voice = normalize_voice_text(voice_text)
    #             st.session_state.voice_input = normalized_voice
    #             st.success(f"인식된 음성: '{normalized_voice}'")
    #         else:
    #             st.error("음성을 명확히 인식하지 못했습니다. 다시 시도해 주세요.")

    voice_text = speech_to_text(
        language='ko', 
        start_prompt="🎙️ 음성 기록 시작", 
        stop_prompt="⏹️ 녹음 중단", 
        key='STT_button'
    )

    if voice_text:
        normalized_voice = normalize_voice_text(voice_text)
        st.session_state.voice_input = normalized_voice
        st.success(f"인식 결과: {normalized_voice}")

    # 📝 우선 저장 -> 저장 후 편집 흐름
    if "voice_input" in st.session_state:
        with st.expander("📝 기록 내용 미리보기", expanded=True):
            parsed_results = parse_to_records(st.session_state.voice_input, datetime.now())
            preview_df = pd.DataFrame(parsed_results)
            if preview_df.empty:
                st.warning("음성에서 기록 가능한 항목을 찾지 못했습니다. 다시 말씀해 주세요.")
            else:
                st.dataframe(preview_df, use_container_width=True, hide_index=True)

                if st.button("✅ 우선 저장"):
                    try:
                        append_records(preview_df)
                        st.session_state.last_saved_ids = preview_df["ID"].tolist()
                        del st.session_state.voice_input
                        st.balloons()
                        st.success("먼저 저장했습니다. 아래에서 방금 저장한 항목을 수정할 수 있어요.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"저장 중 오류가 발생했습니다: {e}")

    if st.session_state.get("last_saved_ids"):
        with st.expander("✏️ 방금 저장한 기록 수정", expanded=True):
            try:
                current_df = load_data()
                if current_df is None or current_df.empty:
                    st.info("수정할 데이터가 없습니다.")
                else:
                    target_ids = set(st.session_state["last_saved_ids"])
                    editable_df = current_df[current_df["ID"].astype(str).isin(target_ids)].copy()
                    if editable_df.empty:
                        st.info("방금 저장한 항목을 찾지 못했습니다.")
                    else:
                        editable_df = editable_df.sort_values("시작시간", ascending=False)
                        edited_saved_df = st.data_editor(
                            editable_df,
                            use_container_width=True,
                            hide_index=True,
                            num_rows="fixed",
                        )
                        if st.button("💾 수정사항 반영"):
                            save_updates_by_id(edited_saved_df)
                            st.success("수정사항을 반영했습니다.")
                            st.rerun()
            except Exception as e:
                st.error(f"수정 화면 로딩 중 오류가 발생했습니다: {e}")

    st.divider()
    
    # 📋 최근 활동 피드 (Reverse Timeline)
    st.subheader("최근 15개 활동")
    try:
        df = load_data()
        if not df.empty:
            icon_map = {"모유": "👩‍🍼", "분유": "🍼", "유축수유": "🥛", "수면": "💤", "기저귀": "💩", "목욕": "🧼", "터미타임": "🤸"}
            df = df.copy()
            raw_df = df.copy()
            
            # 🚨 추가된 부분: Tab 1에서도 '오전/오후'를 'AM/PM'으로 바꿔줍니다.
            df["시작시간"] = df["시작시간"].astype(str).str.replace('오전', 'AM').str.replace('오후', 'PM')
            df["시작시간"] = pd.to_datetime(df["시작시간"], errors="coerce")
            df = df.sort_values("시작시간", ascending=False, na_position="last")

            recent_rows = df.head(15)
            recent_ids = recent_rows["ID"].astype(str).tolist()

            for _, row in recent_rows.iterrows():
                # 분류2에 맞는 아이콘이 없으면 분류1 검색, 둘 다 없으면 기본 아이콘 표시
                icon = icon_map.get(row['분류2'], icon_map.get(row['분류1'], "📝"))
                row_id = str(row.get("ID", ""))
                
                with st.container():
                    c1, c2 = st.columns([0.2, 0.8])
                    if c1.button(icon, key=f"pick_edit_{row_id}", help="이 기록 수정"):
                        st.session_state.selected_edit_id = row_id
                    
                    detail = f"**{row['분류1']} ({row['분류2']})**"
                    if pd.notna(row['분류3']) and row['분류3'] != "": 
                        detail += f" - {row['분류3']}"
                    amt = _safe_float(row["양(ml)"])
                    if amt > 0:
                        detail += f" | {int(amt)}ml"
                    dur_min = _safe_float(row["소요시간(분)"])
                    if dur_min > 0:
                        detail += f" | {int(dur_min)}분"
                    
                    c2.markdown(detail)
                    # 시간 형식 가공 (초 단위 제거)
                    start_str = str(row['시작시간'])[:16]
                    end_str = str(row['종료시간'])[:16]
                    c2.caption(f"🕒 {start_str} ~ {end_str}")
                    st.markdown("---")

            selected_id = st.session_state.get("selected_edit_id")
            if selected_id and selected_id in recent_ids:
                editable_one = raw_df[raw_df["ID"].astype(str) == selected_id].copy()
                if editable_one.empty:
                    st.info("선택한 기록을 찾지 못했습니다.")
                elif hasattr(st, "dialog"):
                    @st.dialog("✏️ 선택한 활동 수정")
                    def edit_single_record_dialog():
                        edited_one_df = st.data_editor(
                            editable_one,
                            use_container_width=True,
                            hide_index=True,
                            num_rows="fixed",
                            key=f"single_editor_{selected_id}",
                        )
                        c_save, c_close = st.columns([1, 1])
                        if c_save.button("💾 수정 반영", key=f"save_single_{selected_id}"):
                            try:
                                save_updates_by_id(edited_one_df)
                                st.session_state.pop("selected_edit_id", None)
                                st.success("선택한 기록 수정사항을 반영했습니다.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"수정 반영 중 오류가 발생했습니다: {e}")
                        if c_close.button("닫기", key=f"close_single_{selected_id}"):
                            st.session_state.pop("selected_edit_id", None)
                            st.rerun()

                    edit_single_record_dialog()
                else:
                    # 구버전 Streamlit 폴백: dialog 미지원 시 인라인 편집
                    with st.expander("✏️ 선택한 활동 수정", expanded=True):
                        edited_one_df = st.data_editor(
                            editable_one,
                            use_container_width=True,
                            hide_index=True,
                            num_rows="fixed",
                            key=f"single_editor_{selected_id}",
                        )
                        c_save, c_close = st.columns([1, 1])
                        if c_save.button("💾 수정 반영", key=f"save_single_{selected_id}"):
                            try:
                                save_updates_by_id(edited_one_df)
                                st.session_state.pop("selected_edit_id", None)
                                st.success("선택한 기록 수정사항을 반영했습니다.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"수정 반영 중 오류가 발생했습니다: {e}")
                        if c_close.button("닫기", key=f"close_single_{selected_id}"):
                            st.session_state.pop("selected_edit_id", None)
                            st.rerun()
        else:
            st.info("아직 기록된 데이터가 없습니다.")
    except Exception as e:
        st.warning(
            f"데이터를 불러오지 못했습니다. ({e}) 구글 시트 연동 설정을 확인해 주세요."
        )

# --- 5. 통계 화면 (Pandas 활용) ---
with tab2:
    st.title("📈 윤겸이 활동 대시보드")
    with st.expander("⚙️ 수유량 환산 설정", expanded=False):
        st.slider(
            "👩‍🍼 모유 1분당 환산량 (ml)",
            min_value=5,
            max_value=20,
            value=10,
            key="breast_milk_ml_per_min",
            help="모유 기록의 소요시간(분) × 환산량으로 일별 수유량에 반영됩니다.",
        )
    try:
        df_stats = load_data()
        if not df_stats.empty:
            # 🚨 추가된 부분: 한글 '오전/오후'를 'AM/PM'으로 먼저 바꿔줍니다.
            df_stats['시작시간'] = df_stats['시작시간'].astype(str).str.replace('오전', 'AM').str.replace('오후', 'PM')
            df_stats['시작시간'] = pd.to_datetime(df_stats['시작시간'], errors='coerce')
            
            df_stats = df_stats.dropna(subset=['시작시간'])
            
            df_stats['날짜'] = df_stats['시작시간'].dt.date
            
            col1, col2 = st.columns(2)
            
            with col1:
                # 1. 일별 수유량 합계 (모유 환산 + 유축수유 + 분유)
                st.subheader("🍼 일별 수유량 합계 (ml)")
                rate = st.session_state.get("breast_milk_ml_per_min", 10)
                feed_base = df_stats[df_stats['분류1'] == '수유'].copy()
                feed_base["양_num"] = pd.to_numeric(feed_base["양(ml)"], errors="coerce").fillna(0)
                feed_base["분_num"] = pd.to_numeric(feed_base["소요시간(분)"], errors="coerce").fillna(0)
                feed_base["환산양(ml)"] = 0.0

                is_breast = feed_base["분류2"] == "모유"
                is_other_milk = feed_base["분류2"].isin(["분유", "유축수유"])
                feed_base.loc[is_breast, "환산양(ml)"] = feed_base.loc[is_breast, "분_num"] * rate
                feed_base.loc[is_other_milk, "환산양(ml)"] = feed_base.loc[is_other_milk, "양_num"]

                feed_df = feed_base.groupby('날짜')['환산양(ml)'].sum()
                st.bar_chart(feed_df)
                st.caption(f"모유 환산 기준: 1분당 {rate}ml")
                
            with col2:
                # 2. 일별 수면 시간 합계
                st.subheader("💤 일별 총 수면 시간 (분)")
                sleep_df = df_stats[df_stats['분류1'] == '수면'].groupby('날짜')['소요시간(분)'].sum()
                st.line_chart(sleep_df)
            
            st.divider()
            
            # 3. 모유 좌/우 수유 밸런스 집계
            st.subheader("⚖️ 모유 좌/우 수유 밸런스 (누적 시간)")
            breast_df = df_stats[df_stats['분류2'] == '모유'].groupby('분류3')['소요시간(분)'].sum().reset_index()
            # 간단한 데이터프레임 시각화
            st.dataframe(breast_df, use_container_width=True, hide_index=True)
            
        else:
            st.info("통계를 표시할 충분한 데이터가 없습니다.")
    except Exception as e:
        st.write("데이터 집계 중 오류 발생:", e)
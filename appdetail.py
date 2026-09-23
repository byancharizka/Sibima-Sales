import os
import logging
from io import BytesIO
from datetime import datetime, date

import pandas as pd
import plotly.express as px
import pytz
import requests
import streamlit as st
import plotly.graph_objects as go
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import re

# =========================================================
# 1) PAGE CONFIG - WAJIB PALING ATAS
# =========================================================
st.set_page_config(
    layout="wide",
    page_title="SIBIMA Performance Dashboard - SALES",
    initial_sidebar_state="expanded"
)

# =========================================================
# 2) LOGGING CONFIG
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# =========================================================
# 3) APP CONFIG
# =========================================================
TIMEZONE = pytz.timezone("Asia/Jakarta")
# Ambil tanggal hari ini
today = date.today()

# Default: tanggal 1 bulan aktif sampai hari ini
DEFAULT_START_DATE = date(today.year, today.month, 1)
DEFAULT_END_DATE = today
REQUEST_TIMEOUT = int(os.getenv("SIBIMA_API_TIMEOUT", "120"))
SO_BALANCE_BASE_START_DATE = date(2026, 1, 1)


BASE_URL = {
    "outstanding": "https://erp.sibima.id/api/dashboard/",
    "erp": "https://erp.sibima.id/api/",
    "brp": "https://brp.sibima.id/api/"
}

API_TOKEN = os.getenv("SIBIMA_API_TOKEN", "3bd1c8f44fa6ba220af7382c57c547a9673b0f6f5ada977b850d7f5215e6")

# Pastikan setiap URL diakhiri dengan "/"
for key in BASE_URL:
    if not BASE_URL[key].endswith("/"):
        BASE_URL[key] += "/"

def create_session():
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[502, 503, 504, 429],
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# =========================================================
# 4) CSS CUSTOM
# =========================================================
st.markdown("""
<style>
/* ====== TITLE UTAMA ====== */
h1 {
    font-size: 1.5rem !important;   /* paling besar */
    font-weight: 800;
    color: #222;
}

/* ====== SUBTITLE & SUBHEADER ====== */
h2, h3, h4, h5, h6 {
    font-size: 1rem !important;   /* lebih kecil dari h1 */
    font-weight: 600;
    color: #444;
}

/* ====== LAYOUT CONTAINER ====== */
.block-container {
    padding-top: 2rem;
    padding-bottom: 1rem;
    padding-left: 2rem;
    padding-right: 2rem;
    max-width: 100%;
}

/* ====== METRIC COMPONENTS ====== */
[data-testid="stMetricLabel"] {
    font-size: 0.7rem !important;
}
[data-testid="stMetricValue"] {
    font-size: 0.5rem !important;
}

/* ====== CUSTOM METRIC CARD ====== */
.metric-card {
    background-color: #f4f4f4;
    border: 1px solid #dcdcdc;
    border-radius: 12px;
    padding: 2px;
    box-shadow: 1px 2px 8px rgba(0,0,0,0.05);
    text-align: center;
    margin-top: 3px;
    margin-bottom: 7px;
    margin-left: 2.5px;
    font-size: 0.75rem;
}
            
.metric-card div {
    font-size: 0.67rem !important;
}            

/* ====== SMALL NOTES ====== */
.small-note {
    color: #666;
    font-size: 0.70rem;
}
            
h3, h4, h5 {
    margin-bottom: 0.1rem !important;
}

/* Kurangi jarak antar komponen container */
div[data-testid="stVerticalBlock"] {
    margin-top: 0.1rem !important;
    margin-bottom: 0.1rem !important;
}

/* Kurangi padding default di dalam container */
div[data-testid="stContainer"] {
    padding-top: 0.1rem !important;
    padding-bottom: 0.1rem !important;
}
            

/* ====== FILTER INPUTS ====== */
div[data-testid="stDateInput"], 
div[data-testid="stTextInput"] {
    font-size: 0.7rem !important;   /* ukuran teks lebih kecil */
}

label, .stTextInput label, .stDateInput label {
    font-size: 0.7rem !important;   /* label input lebih kecil */
    color: #555 !important;
}

/* Kurangi tinggi box input agar lebih ramping */
input, textarea {
    font-size: 0.7rem !important;
    padding: 4px 6px !important;
}
            
@media (max-width: 768px) {
    h1 { font-size: 1.2rem !important; }
    h2, h3, h4 { font-size: 0.9rem !important; }
    .metric-card {
        font-size: 0.65rem !important;
        padding: 4px !important;
    }
    [data-testid="stMetricValue"] {
        font-size: 0.7rem !important;
    }
    .block-container {
        padding-left: 0.5rem !important;
        padding-right: 0.5rem !important;
    }
}

                        
</style>
""", unsafe_allow_html=True)


# =========================================================
# 5) UTILITIES
# =========================================================
def metric_card(label: str, value: str):
    st.markdown(
        f"""
        <div class="metric-card">
            <div style="color: #666; font-size: 0.95rem;">{label}</div>
            <div style="font-size: 0.9rem; font-weight: 700; color: #222;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Pastikan semua kolom ada agar operasi berikutnya aman."""
    if df.empty:
        for col in columns:
            if col not in df.columns:
                df[col] = pd.Series(dtype="object")
        return df

    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def safe_to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Konversi kolom ke numerik dengan aman."""
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def safe_to_datetime(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Konversi kolom tanggal dengan aman dan hilangkan timezone."""
    if col in df.columns:
        df[col] = pd.to_datetime(df[col], errors="coerce")
        try:
            df[col] = df[col].dt.tz_localize(None)
        except Exception:
            pass
    return df


def normalize_text_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Normalisasi string agar aman untuk pencarian."""
    for col in columns:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).str.strip()
    return df


def safe_unique_count(df: pd.DataFrame, col: str) -> int:
    if df.empty or col not in df.columns:
        return 0
    return df[col].nunique(dropna=True)


def safe_mean(df: pd.DataFrame, col: str) -> float:
    if df.empty or col not in df.columns:
        return 0.0
    return float(df[col].mean()) if not df[col].dropna().empty else 0.0

def safe_sum(df: pd.DataFrame, col: str) -> float:
    if df.empty:
        return 0.0
    if col not in df.columns:
        # fallback ke kolom lain yang mirip
        for alt in ["Nominal", "discount", "price"]:
            if alt in df.columns:
                col = alt
                break
    return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())



def to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Data") -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    return output.getvalue()


def _safe_numeric_series(
    df: pd.DataFrame,
    column: str,
    default: float = 0.0,
) -> pd.Series:
    """
    Return numeric Series dengan index yang sama seperti dataframe.

    Penting untuk payload API: bila kolom tidak ada, jangan return scalar 0,
    karena scalar tidak mempunyai method .fillna(), .clip(), dsb.
    """
    if df is None:
        return pd.Series(dtype="float64")

    if column not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")

    return (
        pd.to_numeric(df[column], errors="coerce")
        .fillna(default)
        .astype(float)
    )



def _calc_line_nominal_for_debug(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    qty = _safe_numeric_series(df, "item_quantity", 0.0)
    price = _safe_numeric_series(df, "item_price", 0.0)
    disc = _safe_numeric_series(df, "item_discount", 0.0)
    tax = _safe_numeric_series(df, "item_tax1_percentage", 0.0)
    disc_unit = price * (disc / 100.0)
    tax_unit = (price - disc_unit) * (tax / 100.0)
    return (qty * (price - disc_unit + tax_unit)).fillna(0)


def build_revenue_debug_package(source_label: str, snapshots: dict[str, pd.DataFrame], app_total_revenue: float):
    summary_rows = []
    prepared_tabs = {}
    for stage, frame in snapshots.items():
        df = frame.copy() if frame is not None else pd.DataFrame()
        df["Debug Revenue Row"] = _calc_line_nominal_for_debug(df) if not df.empty else pd.Series(dtype="float64")
        prepared_tabs[stage] = df
        date_series = pd.to_datetime(df.get("transaction_date"), errors="coerce") if "transaction_date" in df.columns else pd.Series(dtype="datetime64[ns]")
        summary_rows.append({
            "Stage": stage,
            "Rows": len(df),
            "Unique SI": int(df["transaction_number_si"].nunique()) if "transaction_number_si" in df.columns else 0,
            "Unique SI Detail": int(df["si_detail_id"].nunique()) if "si_detail_id" in df.columns else 0,
            "Unique Customer": int(df["Customer"].nunique()) if "Customer" in df.columns else 0,
            "Total Qty": float(_safe_numeric_series(df, "item_quantity", 0.0).sum()) if not df.empty else 0.0,
            "Calculated Revenue": float(pd.to_numeric(df["Debug Revenue Row"], errors="coerce").fillna(0).sum()) if not df.empty else 0.0,
            "Min Date": date_series.min() if not date_series.empty else pd.NaT,
            "Max Date": date_series.max() if not date_series.empty else pd.NaT,
        })
    summary = pd.DataFrame(summary_rows)
    final_calc = float(prepared_tabs.get("FINAL_REVENUE", pd.DataFrame()).get("Debug Revenue Row", pd.Series(dtype=float)).sum()) if "FINAL_REVENUE" in prepared_tabs else 0.0
    gap = final_calc - float(app_total_revenue or 0)
    summary["Card Revenue"] = float(app_total_revenue or 0)
    summary["Final Debug vs Card Gap"] = gap
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        summary.to_excel(writer, index=False, sheet_name="SUMMARY")
        for stage, df in prepared_tabs.items():
            sheet = {"RAW_SOURCE":"RAW_SI","PERIOD_FILTERED":"PERIOD_SI","STATUS_VALID":"STATUS_SI","FINAL_REVENUE":"FINAL_REVENUE"}.get(stage, stage[:31])
            df.to_excel(writer, index=False, sheet_name=sheet[:31])
    return summary, output.getvalue(), gap


def build_so_balance_debug_package(source_label: str, raw_df: pd.DataFrame, filtered_df: pd.DataFrame):
    """Audit SO Balance DO-only: NO DO + PARTIAL DO."""
    rows = []
    tabs = {}
    for stage, frame in [("RAW_BALANCE", raw_df), ("AFTER_SEARCH", filtered_df)]:
        df = frame.copy() if frame is not None else pd.DataFrame()
        tabs[stage] = df
        rows.append({
            "Stage": stage,
            "Rows": len(df),
            "Unique SO": int(df["No. SO"].nunique()) if "No. SO" in df.columns else 0,
            "Unique SO Detail": int(df["SO Detail ID"].nunique()) if "SO Detail ID" in df.columns else 0,
            "Nominal Balance": float(_safe_numeric_series(df, "Nominal", 0.0).sum()) if not df.empty else 0.0,
            "NO DO Rows": int((df.get("Balance Type", pd.Series(index=df.index, dtype="object")) == "NO DO").sum()) if not df.empty else 0,
            "PARTIAL DO Rows": int((df.get("Balance Type", pd.Series(index=df.index, dtype="object")) == "PARTIAL DO").sum()) if not df.empty else 0,
            "Min Date": pd.to_datetime(df.get("transaction_date"), errors="coerce").min() if "transaction_date" in df.columns else pd.NaT,
            "Max Date": pd.to_datetime(df.get("transaction_date"), errors="coerce").max() if "transaction_date" in df.columns else pd.NaT,
        })
    summary = pd.DataFrame(rows)
    status_summary = pd.DataFrame()
    if filtered_df is not None and not filtered_df.empty and "Status" in filtered_df.columns:
        tmp = filtered_df.copy()
        tmp["Nominal"] = _safe_numeric_series(tmp, "Nominal", 0.0)
        status_summary = tmp.groupby("Status", dropna=False).agg(
            Rows=("Status", "size"),
            Unique_SO=("No. SO", "nunique") if "No. SO" in tmp.columns else ("Status", "size"),
            Nominal=("Nominal", "sum"),
        ).reset_index()
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        summary.to_excel(writer, index=False, sheet_name="SUMMARY")
        if not status_summary.empty:
            status_summary.to_excel(writer, index=False, sheet_name="STATUS")
        for stage, df in tabs.items():
            df.to_excel(writer, index=False, sheet_name=stage[:31])
    return summary, status_summary, output.getvalue()


def build_pic_debug_excel_api(current_so: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        preview_cols = [c for c in ["transaction_number_so", "so_detail_id", "PIC Sales", "Status_so", "transaction_date"] if c in current_so.columns]
        current_so[preview_cols].drop_duplicates().to_excel(writer, index=False, sheet_name="CURRENT_PIC")
        if "PIC Sales" in current_so.columns:
            s = current_so["PIC Sales"].fillna("").astype(str).str.strip()
            summary = s[s.ne("")].value_counts().rename_axis("PIC Sales").reset_index(name="Rows")
            summary.to_excel(writer, index=False, sheet_name="PIC_SUMMARY")
    return output.getvalue()



# =========================================================
# TOTAL SO DEBUG HELPERS
# =========================================================
def _debug_canonical_key(value):
    """Normalisasi key untuk membandingkan hasil PostgreSQL vs API."""
    if value is None or pd.isna(value):
        return ""
    raw = str(value).strip()
    if not raw or raw.lower() in {"nan", "none", "null", "<na>"}:
        return ""
    try:
        number = float(raw)
        if number.is_integer():
            return str(int(number))
    except Exception:
        pass
    return raw


def _prepare_total_so_debug_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Bentuk dataframe audit yang konsisten untuk PostgreSQL maupun API.

    Debug nominal memakai formula yang SAMA dengan card Total SO:
        discount/unit = price * discount%
        tax/unit      = (price - discount/unit) * tax1%
        net/unit      = price - discount/unit + tax/unit
        nominal       = qty * net/unit
    """
    if df is None:
        df = pd.DataFrame()

    out = df.copy()

    required = [
        "transaction_number_so", "transaction_date", "Status_so",
        "so_detail_id", "product_id", "item_name", "PIC Sales",
        "item_quantity", "item_price", "item_discount",
        "item_tax1_percentage",
    ]
    for col in required:
        if col not in out.columns:
            out[col] = pd.NA

    out["transaction_date"] = pd.to_datetime(
        out["transaction_date"], errors="coerce"
    )

    # Samakan vocabulary status pada file debug.
    out["Status_so"] = (
        out["Status_so"]
        .map(normalize_api_status)
        .fillna("")
        .astype(str)
        .str.strip()
    )

    def numeric(col):
        return pd.to_numeric(out[col], errors="coerce").fillna(0)

    qty = numeric("item_quantity")
    price = numeric("item_price")
    discount_pct = numeric("item_discount")
    tax1_pct = numeric("item_tax1_percentage")

    out["Debug Disc Per Unit"] = price * (discount_pct / 100.0)
    out["Debug Tax Per Unit"] = (
        price - out["Debug Disc Per Unit"]
    ) * (tax1_pct / 100.0)
    out["Debug Net Price Unit"] = (
        price - out["Debug Disc Per Unit"] + out["Debug Tax Per Unit"]
    )
    out["Debug Total SO Row"] = qty * out["Debug Net Price Unit"]

    out["Debug SO Key"] = out["transaction_number_so"].map(_debug_canonical_key)
    out["Debug Detail Key"] = out["so_detail_id"].map(_debug_canonical_key)
    out["Debug Product Key"] = out["product_id"].map(_debug_canonical_key)
    out["Compare Key"] = (
        out["Debug SO Key"]
        + "|" + out["Debug Detail Key"]
        + "|" + out["Debug Product Key"]
    )

    return out


def _total_so_debug_summary_row(stage: str, df: pd.DataFrame) -> dict:
    prepared = _prepare_total_so_debug_df(df)

    if prepared.empty:
        return {
            "Stage": stage,
            "Rows": 0,
            "Unique SO": 0,
            "Unique SO Detail": 0,
            "Unique Product": 0,
            "Duplicate Compare-Key Rows": 0,
            "Null Detail ID": 0,
            "Null Product ID": 0,
            "Total Qty": 0.0,
            "Calculated Nominal": 0.0,
            "Min Transaction Date": "-",
            "Max Transaction Date": "-",
        }

    valid_compare_key = (
        prepared["Debug SO Key"].ne("")
        & prepared["Debug Detail Key"].ne("")
        & prepared["Debug Product Key"].ne("")
    )
    duplicate_rows = int(
        (
            valid_compare_key
            & prepared.duplicated(subset=["Compare Key"], keep=False)
        ).sum()
    )

    dates = prepared["transaction_date"].dropna()
    min_date = dates.min().strftime("%Y-%m-%d") if not dates.empty else "-"
    max_date = dates.max().strftime("%Y-%m-%d") if not dates.empty else "-"

    return {
        "Stage": stage,
        "Rows": int(len(prepared)),
        "Unique SO": int(prepared["Debug SO Key"].replace("", pd.NA).nunique(dropna=True)),
        "Unique SO Detail": int(prepared["Debug Detail Key"].replace("", pd.NA).nunique(dropna=True)),
        "Unique Product": int(prepared["Debug Product Key"].replace("", pd.NA).nunique(dropna=True)),
        "Duplicate Compare-Key Rows": duplicate_rows,
        "Null Detail ID": int(prepared["Debug Detail Key"].eq("").sum()),
        "Null Product ID": int(prepared["Debug Product Key"].eq("").sum()),
        "Total Qty": float(pd.to_numeric(prepared["item_quantity"], errors="coerce").fillna(0).sum()),
        "Calculated Nominal": float(prepared["Debug Total SO Row"].sum()),
        "Min Transaction Date": min_date,
        "Max Transaction Date": max_date,
    }


def build_total_so_debug_package(
    source_label: str,
    snapshots: dict[str, pd.DataFrame],
    app_total_so: float,
) -> tuple[pd.DataFrame, bytes, float]:
    """
    Buat summary + workbook audit Total SO.

    Workbook dari PostgreSQL dan API memiliki struktur sheet yang sama sehingga
    tab FINAL_COMPARE dapat dibandingkan menggunakan kolom `Compare Key`.
    """
    prepared = {
        stage: _prepare_total_so_debug_df(df)
        for stage, df in snapshots.items()
    }

    summary = pd.DataFrame([
        _total_so_debug_summary_row(stage, df)
        for stage, df in snapshots.items()
    ])

    final_df = prepared.get("FINAL_TOTAL_SO", pd.DataFrame()).copy()
    final_debug_nominal = (
        float(final_df["Debug Total SO Row"].sum())
        if not final_df.empty and "Debug Total SO Row" in final_df.columns
        else 0.0
    )
    gap_vs_card = final_debug_nominal - float(app_total_so or 0)

    summary["Source"] = source_label
    summary["Card Total SO"] = float(app_total_so or 0)
    summary["Final Debug vs Card Gap"] = gap_vs_card

    after_search = prepared.get("AFTER_SEARCH_FILTER", pd.DataFrame()).copy()
    if not after_search.empty:
        status_breakdown = (
            after_search.groupby("Status_so", dropna=False)
            .agg(
                Rows=("Compare Key", "size"),
                Unique_SO=("Debug SO Key", lambda s: s.replace("", pd.NA).nunique(dropna=True)),
                Unique_Detail=("Debug Detail Key", lambda s: s.replace("", pd.NA).nunique(dropna=True)),
                Calculated_Nominal=("Debug Total SO Row", "sum"),
            )
            .reset_index()
            .sort_values("Calculated_Nominal", ascending=False)
        )
    else:
        status_breakdown = pd.DataFrame(
            columns=["Status_so", "Rows", "Unique_SO", "Unique_Detail", "Calculated_Nominal"]
        )

    # Duplicate key dicek setelah date + search filter, yaitu grain yang paling
    # relevan sebelum status/keyword Total SO diterapkan.
    if not after_search.empty:
        valid_key = (
            after_search["Debug SO Key"].ne("")
            & after_search["Debug Detail Key"].ne("")
            & after_search["Debug Product Key"].ne("")
        )
        duplicate_keys = after_search[
            valid_key
            & after_search.duplicated(subset=["Compare Key"], keep=False)
        ].copy()
        duplicate_keys = duplicate_keys.sort_values(
            ["Compare Key", "transaction_date"], na_position="last"
        )
    else:
        duplicate_keys = pd.DataFrame()

    status_valid = prepared.get("STATUS_VALID", pd.DataFrame()).copy()
    if not status_valid.empty:
        final_indices = set(final_df.index.tolist()) if not final_df.empty else set()
        excluded_keyword = status_valid[
            ~status_valid.index.isin(final_indices)
        ].copy()
    else:
        excluded_keyword = pd.DataFrame()

    compare_columns = [
        "Compare Key",
        "transaction_number_so",
        "transaction_date",
        "Status_so",
        "so_detail_id",
        "product_id",
        "item_name",
        "PIC Sales",
        "item_quantity",
        "item_price",
        "item_discount",
        "item_tax1_percentage",
        "Debug Disc Per Unit",
        "Debug Tax Per Unit",
        "Debug Net Price Unit",
        "Debug Total SO Row",
    ]
    final_compare = final_df[
        [col for col in compare_columns if col in final_df.columns]
    ].copy() if not final_df.empty else pd.DataFrame(columns=compare_columns)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        summary.to_excel(writer, index=False, sheet_name="SUMMARY")
        status_breakdown.to_excel(writer, index=False, sheet_name="STATUS_BREAKDOWN")
        final_compare.to_excel(writer, index=False, sheet_name="FINAL_COMPARE")
        duplicate_keys.to_excel(writer, index=False, sheet_name="DUPLICATE_KEYS")
        excluded_keyword.to_excel(writer, index=False, sheet_name="EXCLUDED_KEYWORD")

        sheet_map = {
            "RAW_SOURCE": "RAW_SOURCE",
            "PERIOD_FILTERED": "PERIOD_FILTERED",
            "AFTER_SEARCH_FILTER": "AFTER_SEARCH",
            "STATUS_VALID": "STATUS_VALID",
            "FINAL_TOTAL_SO": "FINAL_TOTAL_SO",
        }
        for stage, sheet_name in sheet_map.items():
            prepared.get(stage, pd.DataFrame()).to_excel(
                writer,
                index=False,
                sheet_name=sheet_name,
            )

    return summary, output.getvalue(), gap_vs_card


# =========================================================
# 6) API FETCHING
# =========================================================
@st.cache_data(ttl=300, show_spinner=False)
def get_api_data_old(endpoint: str, source: str = "outstanding", start_date=None, end_date=None):
    base_url = BASE_URL.get(source, BASE_URL["outstanding"])
    url = f"{base_url}{endpoint}"
    params = {"date_start": start_date, "date_end": end_date}

    try:
        logger.info("Fetching endpoint=%s from source=%s params=%s", endpoint, source, params)

        # 🔹 Gunakan session dengan retry
        session = create_session()
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)

        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict):
            data_layer = payload.get("data", {})
            if isinstance(data_layer, dict):
                rows = data_layer.get("data", [])
                if isinstance(rows, list):
                    df = pd.DataFrame(rows)
                    df = safe_to_datetime(df, "transaction_date")
                    return df
        return pd.DataFrame()

    except Exception as e:
        st.warning(f"Gagal mengambil data dari endpoint {endpoint} ({source}): {e}")
        return pd.DataFrame()

@st.cache_data(ttl=300, show_spinner=False)
def get_api_data_new(endpoint: str, source: str = "erp", start_date=None, end_date=None):
    base_url = BASE_URL.get(source, BASE_URL["erp"])
    url = f"{base_url}{endpoint}"
    params = {
        "date_start": start_date,
        "date_end": end_date,
        "token": API_TOKEN
    }

    try:
        # 🔹 Gunakan session dengan retry
        session = create_session()
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)

        response.raise_for_status()
        payload = response.json()

        rows = payload.get("data", [])
        if isinstance(rows, list):
            all_rows = []
            for row in rows:
                items = row.get("items", [])
                if items:
                    for item in items:
                        flat = {**row, **{f"item_{k}": v for k, v in item.items()}}
                        all_rows.append(flat)
                else:
                    all_rows.append(row)

            df = pd.DataFrame(all_rows)
            df = safe_to_datetime(df, "transaction_date")
            return df

        return pd.DataFrame()

    except Exception as e:
        st.warning(f"Gagal mengambil data dari endpoint {endpoint} ({source}): {e}")
        return pd.DataFrame()


def load_all_data(start_date=None, end_date=None) -> dict[str, pd.DataFrame]:
    """Outstanding endpoints lama selain SO. SO Balance dibangun ulang dari raw SO + DO."""
    endpoint_map = {
        "pr": ("pr-balance", {"Tgl. PR": "transaction_date"}),
        "po": ("po-balance", {"Tgl. PO": "transaction_date"}),
        "grn": ("grn-balance", {"Tgl. GRN": "transaction_date"}),
        "do": ("do-balance", {"Tgl. DO": "transaction_date"}),
        "npr": ("outstanding-npr", {"Tanggal": "transaction_date"}),
    }
    result = {"so": pd.DataFrame()}
    for key, (endpoint, rename_map) in endpoint_map.items():
        df = get_api_data_old(endpoint, source="outstanding", start_date=start_date, end_date=end_date)
        if not df.empty:
            df = df.rename(columns=rename_map)
            df = safe_to_datetime(df, "transaction_date")
        result[key] = df
    return result



def load_all_data_new(start_date=None, end_date=None) -> dict[str, pd.DataFrame]:
    # Mapping endpoint baru sesuai API kamu
    endpoint_map_new = {
        "so": ("sales-orders", {"date" : "transaction_date"}),
        "pr": ("purchase-requests",{}),
        "po": ("purchase-orders", {"date" : "transaction_date"}),
        "grn" : ("goods-receipt-notes", {}),
        "do": ("delivery-orders",{}),
        "si": ("sales-invoices",{})
    }

    result_new = {}
    for key, (endpoint, rename_map_new) in endpoint_map_new.items():
        df = get_api_data_new(endpoint, source="erp", start_date=start_date, end_date=end_date)

        if not df.empty:
            df = df.rename(columns=rename_map_new)
            df = safe_to_datetime(df, "transaction_date")
        result_new[key] = df

    return result_new


@st.cache_data(ttl=300, show_spinner=False)
def load_so_balance_source_api(start_date=None, end_date=None) -> dict[str, pd.DataFrame]:
    """Load hanya raw SO + DO untuk membangun SO Balance DO-only."""
    result = {}
    for key, endpoint, rename_map in [
        ("so", "sales-orders", {"date": "transaction_date"}),
        ("do", "delivery-orders", {}),
    ]:
        df = get_api_data_new(
            endpoint, source="erp", start_date=start_date, end_date=end_date
        )
        if not df.empty:
            df = df.rename(columns=rename_map)
            df = safe_to_datetime(df, "transaction_date")
        result[key] = df
    return result


# =========================================================
# STATUS COMPATIBILITY: API/DB CODE -> LABEL
# =========================================================
DB_STATUS_TO_API_LABEL = {
    0: "Draft",
    1: "Need Approve",
    2: "Approved",
    3: "In Progress",
    4: "Complete",
    5: "Approved",
    6: "Approved",
    7: "Close",
}


def normalize_api_status(value):
    """Normalisasi status agar logic Total SO konsisten dengan versi PostgreSQL."""
    if value is None or pd.isna(value):
        return ""

    raw = str(value).strip()
    if not raw or raw.lower() in {"nan", "none", "null", "<na>"}:
        return ""

    try:
        number = float(raw)
        if number.is_integer():
            code = int(number)
            if code in DB_STATUS_TO_API_LABEL:
                return DB_STATUS_TO_API_LABEL[code]
    except Exception:
        pass

    key = raw.lower().replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    aliases = {
        "draft": "Draft",
        "need approve": "Need Approve",
        "need approved": "Need Approve",
        "need approval": "Need Approve",
        "pending approval": "Need Approve",
        "approved": "Approved",
        "approved1": "Approved",
        "approved 1": "Approved",
        "approved2": "Approved",
        "approved 2": "Approved",
        "in progress": "In Progress",
        "inprogress": "In Progress",
        "complete": "Complete",
        "completed": "Complete",
        "closed": "Close",
        "close": "Close",
    }
    return aliases.get(key, raw)


SO_BALANCE_EXCLUDED_STATUSES = {"Draft", "Need Approve", "Complete"}


def _canonical_db_key(value):
    if value is None or pd.isna(value):
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"nan", "none", "null", "<na>"}:
        return None
    try:
        f = float(raw)
        if f.is_integer():
            return str(int(f))
    except Exception:
        pass
    return raw


def _join_unique_text(series: pd.Series) -> str:
    values = []
    for value in series.dropna():
        s = str(value).strip()
        if s and s.lower() not in {"nan", "none", "null", "<na>"} and s not in values:
            values.append(s)
    return " | ".join(values)


def _so_net_unit_price(df: pd.DataFrame) -> pd.Series:
    """
    Harga satuan netto SO yang aman untuk payload API yang tidak selalu
    mengirim seluruh kolom detail.

    Formula utama:
        Discount/unit = Price * Discount %
        Tax/unit      = (Price - Discount/unit) * Tax1 %
        Net unit      = Price - Discount/unit + Tax/unit

    Jika formula utama menghasilkan 0 tetapi API menyediakan subtotal,
    subtotal / qty dipakai sebagai fallback.
    """
    if df is None or df.empty:
        return pd.Series(dtype="float64")

    qty = _safe_numeric_series(df, "item_quantity", 0.0)
    price = _safe_numeric_series(df, "item_price", 0.0)
    discount = _safe_numeric_series(df, "item_discount", 0.0)
    tax1 = _safe_numeric_series(df, "item_tax1_percentage", 0.0)
    subtotal = _safe_numeric_series(df, "item_sub_total", 0.0)

    disc_per_unit = price * (discount / 100.0)
    taxable = price - disc_per_unit
    tax_per_unit = taxable * (tax1 / 100.0)
    net = taxable + tax_per_unit

    fallback = pd.Series(0.0, index=df.index, dtype="float64")
    valid_qty = qty > 0
    fallback.loc[valid_qty] = subtotal.loc[valid_qty] / qty.loc[valid_qty]

    use_fallback = (net == 0) & (fallback != 0)
    net.loc[use_fallback] = fallback.loc[use_fallback]
    return net.fillna(0)


def _build_so_balance_view(
    so_df: pd.DataFrame,
    do_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    SO BALANCE — BUSINESS RULE FINAL (DO ONLY)

    Definisi:
      1) SO yang BELUM memiliki DO valid -> NO DO
      2) SO yang sudah memiliki DO tetapi qty DO masih < qty SO -> PARTIAL DO
      3) SO yang qty DO-nya sudah >= qty SO -> TIDAK masuk SO Balance

    PR dan SI TIDAK dipakai untuk mengurangi SO Balance.

    DO valid untuk progress delivery:
      - Approved
      - In Progress
      - Complete
      - Close

    DO Draft / Need Approve tidak dianggap sebagai delivery progress.

    Effective DO Qty:
      - direct DO detail yang terhubung ke SO detail adalah source utama;
      - SO detail do_quantity dipakai sebagai fallback/cross-check bila direct link
        tidak tersedia atau lebih kecil;
      - SI quantity tidak dipakai.

    Balance Qty = MAX(SO Qty - Effective DO Qty, 0)
    SO Balance  = Balance Qty * Net Unit Price SO
    """
    output_cols = [
        "No. SO", "PIC Sales", "Status", "Nominal", "transaction_date",
        "SO Detail ID", "Product ID", "Item Name",
        "SO Qty", "SO Detail DO Qty", "DO Qty (Direct Valid)",
        "Effective DO Qty", "DO Qty", "Balance Qty",
        "Unit Price", "Discount %", "Tax1 %", "Net Unit Price",
        "SO Nominal", "Delivered Nominal Proxy",
        "Balance Type", "Quantity Source", "Diagnostic",
        "SO Detail DO Status", "Realized Qty (Diagnostic)",
        "DO Documents", "DO Detail Count", "DO Statuses",
    ]

    if so_df is None or so_df.empty:
        return pd.DataFrame(columns=output_cols)

    so = so_df.copy()
    do = do_df.copy() if do_df is not None else pd.DataFrame()

    # ---------------------------------------------------------
    # SO grain: satu row per current SO detail + product
    # ---------------------------------------------------------
    so["__so_detail_key"] = so.get(
        "item_id", pd.Series(index=so.index, dtype="object")
    ).map(_canonical_db_key)
    so["__product_key"] = so.get(
        "item_product_id", pd.Series(index=so.index, dtype="object")
    ).map(_canonical_db_key)
    so["__so_qty"] = _safe_numeric_series(
        so, "item_quantity", 0.0
    ).clip(lower=0)
    so["__net_unit_price"] = _so_net_unit_price(so)
    so["__so_do_qty"] = _safe_numeric_series(
        so, "item_do_quantity", 0.0
    ).clip(lower=0)
    so["__realized_qty_diag"] = _safe_numeric_series(
        so, "item_realized_quantity", 0.0
    ).clip(lower=0)
    so["__so_do_status_norm"] = so.get(
        "item_do_status", pd.Series("", index=so.index, dtype="object")
    ).map(normalize_api_status).fillna("")

    valid_key = so["__so_detail_key"].notna()
    so_valid = so.loc[valid_key].drop_duplicates(
        subset=["__so_detail_key", "__product_key"], keep="first"
    )
    so_invalid = so.loc[~valid_key].copy()
    so = pd.concat([so_valid, so_invalid], ignore_index=True, sort=False)

    # ---------------------------------------------------------
    # DO progress: ONLY valid DO statuses.
    # Strict detail+product diprioritaskan; detail-only menjadi fallback.
    # ---------------------------------------------------------
    valid_do_statuses = {"Approved", "In Progress", "Complete", "Close"}

    do_strict = pd.DataFrame(columns=[
        "__so_detail_key", "__product_key", "__direct_do_qty_strict",
        "__do_docs_strict", "__do_detail_count_strict", "__do_statuses_strict",
    ])
    do_detail_only = pd.DataFrame(columns=[
        "__so_detail_key", "__direct_do_qty_detail",
        "__do_docs_detail", "__do_detail_count_detail", "__do_statuses_detail",
    ])
    do_any_count = pd.DataFrame(columns=["__so_detail_key", "__linked_do_any_count"])

    if not do.empty:
        do["__so_detail_key"] = do.get(
            "item_so_detail_id", pd.Series(index=do.index, dtype="object")
        ).map(_canonical_db_key)
        do["__product_key"] = do.get(
            "item_product_id", pd.Series(index=do.index, dtype="object")
        ).map(_canonical_db_key)
        do["__direct_do_qty"] = _safe_numeric_series(
            do, "item_quantity", 0.0
        ).clip(lower=0)
        do["__do_status_norm"] = do.get(
            "status_description", pd.Series("", index=do.index, dtype="object")
        ).map(normalize_api_status).fillna("")

        do_linked_all = do[do["__so_detail_key"].notna()].copy()
        if not do_linked_all.empty:
            if "item_id" in do_linked_all.columns:
                do_linked_all["__do_detail_key"] = do_linked_all["item_id"].map(_canonical_db_key)
                do_linked_all = do_linked_all.drop_duplicates(
                    subset=["__do_detail_key"], keep="first"
                )
            do_any_count = (
                do_linked_all.groupby("__so_detail_key", dropna=False)
                .size().reset_index(name="__linked_do_any_count")
            )

        do_valid = do_linked_all[
            do_linked_all["__do_status_norm"].isin(valid_do_statuses)
        ].copy() if not do_linked_all.empty else pd.DataFrame()

        if not do_valid.empty:
            strict_linked = do_valid[do_valid["__product_key"].notna()].copy()
            if not strict_linked.empty:
                do_strict = (
                    strict_linked.groupby(
                        ["__so_detail_key", "__product_key"], dropna=False
                    )
                    .agg(
                        __direct_do_qty_strict=("__direct_do_qty", "sum"),
                        __do_docs_strict=("transaction_number", _join_unique_text),
                        __do_detail_count_strict=("item_id", lambda x: x.dropna().astype(str).nunique())
                        if "item_id" in strict_linked.columns else ("__direct_do_qty", "size"),
                        __do_statuses_strict=("__do_status_norm", _join_unique_text),
                    )
                    .reset_index()
                )

            do_detail_only = (
                do_valid.groupby("__so_detail_key", dropna=False)
                .agg(
                    __direct_do_qty_detail=("__direct_do_qty", "sum"),
                    __do_docs_detail=("transaction_number", _join_unique_text),
                    __do_detail_count_detail=("item_id", lambda x: x.dropna().astype(str).nunique())
                    if "item_id" in do_valid.columns else ("__direct_do_qty", "size"),
                    __do_statuses_detail=("__do_status_norm", _join_unique_text),
                )
                .reset_index()
            )

    balance = (
        so.merge(do_strict, how="left", on=["__so_detail_key", "__product_key"])
          .merge(do_detail_only, how="left", on="__so_detail_key")
          .merge(do_any_count, how="left", on="__so_detail_key")
    )

    for col in [
        "__direct_do_qty_strict", "__direct_do_qty_detail",
        "__do_detail_count_strict", "__do_detail_count_detail",
        "__linked_do_any_count",
    ]:
        balance[col] = _safe_numeric_series(
            balance, col, 0.0
        )

    # Strict product match first; detail-only is fallback only.
    balance["__direct_do_qty"] = balance["__direct_do_qty_strict"].where(
        balance["__direct_do_qty_strict"] > 0,
        balance["__direct_do_qty_detail"],
    ).fillna(0).clip(lower=0)

    strict_used = balance["__direct_do_qty_strict"] > 0
    balance["__do_docs"] = balance.get(
        "__do_docs_detail", pd.Series("", index=balance.index, dtype="object")
    ).fillna("")
    balance.loc[strict_used, "__do_docs"] = balance.loc[
        strict_used, "__do_docs_strict"
    ].fillna("")

    balance["__do_statuses"] = balance.get(
        "__do_statuses_detail", pd.Series("", index=balance.index, dtype="object")
    ).fillna("")
    balance.loc[strict_used, "__do_statuses"] = balance.loc[
        strict_used, "__do_statuses_strict"
    ].fillna("")

    balance["__do_detail_count"] = balance["__do_detail_count_detail"]
    balance.loc[strict_used, "__do_detail_count"] = balance.loc[
        strict_used, "__do_detail_count_strict"
    ]
    balance["__do_detail_count"] = balance["__do_detail_count"].fillna(0).astype(int)

    # ---------------------------------------------------------
    # SO detail DO qty = fallback/cross-check.
    # If item_do_status explicitly says Draft/Need Approve, do not count it.
    # If status is blank, keep it as fallback because some ERP rows do not expose
    # item-level DO status even though do_quantity is maintained.
    # ---------------------------------------------------------
    so_detail_do_valid = (
        balance["__so_do_status_norm"].isin(valid_do_statuses)
        | balance["__so_do_status_norm"].eq("")
    )
    balance["__so_do_qty_valid"] = balance["__so_do_qty"].where(
        so_detail_do_valid, 0.0
    )

    # Direct valid DO + SO-detail do_quantity are both operational DO evidence.
    # Use the larger value, then cap to SO Qty.
    balance["__effective_do_raw"] = balance[
        ["__direct_do_qty", "__so_do_qty_valid"]
    ].max(axis=1).fillna(0).clip(lower=0)
    balance["__effective_do_qty"] = balance[
        ["__effective_do_raw", "__so_qty"]
    ].min(axis=1)

    balance["__balance_qty"] = (
        balance["__so_qty"] - balance["__effective_do_qty"]
    ).clip(lower=0)

    # ---------------------------------------------------------
    # SO status rule tetap mengikuti business rule dashboard sebelumnya.
    # ---------------------------------------------------------
    balance["__normalized_so_status"] = (
        balance.get(
            "status_description",
            pd.Series(index=balance.index, dtype="object"),
        )
        .map(normalize_api_status)
        .fillna("")
        .astype(str)
        .str.strip()
    )
    balance = balance[
        ~balance["__normalized_so_status"].isin(SO_BALANCE_EXCLUDED_STATUSES)
    ].copy()

    # Hanya NO DO atau PARTIAL DO. Fully delivered otomatis keluar.
    balance = balance[
        (balance["__so_qty"] > 0) & (balance["__balance_qty"] > 0)
    ].copy()
    if balance.empty:
        return pd.DataFrame(columns=output_cols)

    balance["__balance_type"] = "NO DO"
    partial_mask = (
        (balance["__effective_do_qty"] > 0)
        & (balance["__effective_do_qty"] < balance["__so_qty"])
    )
    balance.loc[partial_mask, "__balance_type"] = "PARTIAL DO"

    balance["__balance_nominal"] = (
        balance["__balance_qty"] * balance["__net_unit_price"]
    )
    balance["__so_nominal"] = (
        balance["__so_qty"] * balance["__net_unit_price"]
    )
    balance["__delivered_nominal_proxy"] = (
        balance["__effective_do_qty"] * balance["__net_unit_price"]
    )

    def quantity_source(row):
        direct = float(row.get("__direct_do_qty") or 0)
        so_do = float(row.get("__so_do_qty_valid") or 0)
        effective = float(row.get("__effective_do_raw") or 0)
        if effective <= 0:
            return "NO VALID DO"
        sources = []
        eps = 1e-9
        if direct > 0 and abs(direct - effective) <= eps:
            sources.append("DO_DETAIL_VALID")
        if so_do > 0 and abs(so_do - effective) <= eps:
            sources.append("SO_DETAIL_DO_QTY")
        return " | ".join(sources) if sources else "DO_PROGRESS"

    balance["__qty_source"] = balance.apply(quantity_source, axis=1)

    def diagnostic(row):
        notes = []
        so_qty = float(row.get("__so_qty") or 0)
        direct = float(row.get("__direct_do_qty") or 0)
        so_do = float(row.get("__so_do_qty_valid") or 0)
        eff = float(row.get("__effective_do_raw") or 0)
        any_do = int(row.get("__linked_do_any_count") or 0)
        if any_do > 0 and direct <= 0:
            notes.append("ONLY DRAFT/NEED APPROVE DO OR NO VALID LINK")
        if abs(direct - so_do) > 1e-9 and (direct > 0 or so_do > 0):
            notes.append("DO QTY CROSS-CHECK MISMATCH")
        if eff > so_qty + 1e-9:
            notes.append("DO QTY > SO QTY; CAPPED")
        if row.get("__balance_type") == "NO DO":
            notes.append("NO VALID DO")
        elif row.get("__balance_type") == "PARTIAL DO":
            notes.append("PARTIAL DO")
        if not notes:
            notes.append("OK")
        return " | ".join(notes)

    balance["__diagnostic"] = balance.apply(diagnostic, axis=1)

    out = pd.DataFrame(index=balance.index)
    out["No. SO"] = balance.get("transaction_number")
    out["PIC Sales"] = balance.get("pic_sales_name")
    out["Status"] = balance["__normalized_so_status"]
    out["Nominal"] = pd.to_numeric(
        balance["__balance_nominal"], errors="coerce"
    ).fillna(0)
    out["transaction_date"] = pd.to_datetime(
        balance.get("transaction_date"), errors="coerce"
    )
    out["SO Detail ID"] = balance.get("item_id")
    out["Product ID"] = balance.get("item_product_id")
    out["Item Name"] = balance.get("item_item_name")
    out["SO Qty"] = balance["__so_qty"]
    out["SO Detail DO Qty"] = balance["__so_do_qty"]
    out["DO Qty (Direct Valid)"] = balance["__direct_do_qty"]
    out["Effective DO Qty"] = balance["__effective_do_qty"]
    out["DO Qty"] = balance["__effective_do_qty"]
    out["Balance Qty"] = balance["__balance_qty"]
    out["Unit Price"] = _safe_numeric_series(
        balance, "item_price", 0.0
    )
    out["Discount %"] = _safe_numeric_series(
        balance, "item_discount", 0.0
    )
    out["Tax1 %"] = _safe_numeric_series(
        balance, "item_tax1_percentage", 0.0
    )
    out["Net Unit Price"] = balance["__net_unit_price"]
    out["SO Nominal"] = balance["__so_nominal"]
    out["Delivered Nominal Proxy"] = balance["__delivered_nominal_proxy"]
    out["Balance Type"] = balance["__balance_type"]
    out["Quantity Source"] = balance["__qty_source"]
    out["Diagnostic"] = balance["__diagnostic"]
    out["SO Detail DO Status"] = balance.get("item_do_status")
    out["Realized Qty (Diagnostic)"] = balance["__realized_qty_diag"]
    out["DO Documents"] = balance["__do_docs"]
    out["DO Detail Count"] = balance["__do_detail_count"]
    out["DO Statuses"] = balance["__do_statuses"]
    return out.reset_index(drop=True)


# =========================================================
# 7) FILTERS & TRANSFORM
# =========================================================
def apply_cumulative_filter(df: pd.DataFrame, end_date_val) -> pd.DataFrame:
    """
    Ambil SEMUA data dari awal hingga end_date.
    """
    if df.empty or "transaction_date" not in df.columns:
        return df.copy()

    working = df.copy()
    working = safe_to_datetime(working, "transaction_date")

    upper_limit = pd.to_datetime(end_date_val).replace(hour=23, minute=59, second=59)
    return working[
        working["transaction_date"].notna() &
        (working["transaction_date"] <= upper_limit)
    ].copy()

def apply_realization_filter(df: pd.DataFrame, start_date_val, end_date_val) -> pd.DataFrame:
    """
    Ambil data hanya dalam rentang tanggal tertentu (start_date sampai end_date).
    Contoh: 1 Mei 2026 s/d 31 Mei 2026.
    """
    if df.empty or "transaction_date" not in df.columns:
        return df.copy()

    working = df.copy()
    working = safe_to_datetime(working, "transaction_date")

    lower_limit = pd.to_datetime(start_date_val).replace(hour=0, minute=0, second=0)
    upper_limit = pd.to_datetime(end_date_val).replace(hour=23, minute=59, second=59)

    return working[
        working["transaction_date"].notna() &
        (working["transaction_date"] >= lower_limit) &
        (working["transaction_date"] <= upper_limit)
    ].copy()



def apply_search_filter(
    df: pd.DataFrame,
    search_number: str = "",
    search_status: str = "Semua Status",
    search_pic: str = "Semua PIC"
) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    working = df.copy()
    working = normalize_text_columns(
        working,
        ["Status", "Status_so", "PIC Sales", "No. PR", "No. DO", "No. PUR", "No. Transaksi"]
    )

    # Filter nomor transaksi
    if search_number:
        pattern = search_number.strip().lower()
        string_cols = working.select_dtypes(include=["object"]).columns.tolist()
        if string_cols:
            mask_number = working[string_cols].apply(
                lambda col: col.str.lower().str.contains(pattern, na=False)
            ).any(axis=1)
            working = working[mask_number]

    # Filter Status khusus SO saja
    if search_status and search_status != "Semua Status":
        if "Status_so" in working.columns:
            working = working[
                working["Status_so"].str.strip().str.lower() == search_status.strip().lower()
            ]

    # Filter PIC Procurement via Dropdown
    if search_pic and search_pic != "Semua PIC":
        pic_cols = [col for col in ["PIC Sales"] if col in working.columns]
        if pic_cols:
            mask_pic = working[pic_cols].apply(
                lambda col: col.str.strip().str.lower() == search_pic.strip().lower()
            ).any(axis=1)
            working = working[mask_pic]

    return working.copy()


def assign_unassigned(df: pd.DataFrame, col: str) -> pd.DataFrame:
    working = df.copy()
    if col in working.columns:
        working[col] = working[col].fillna("Unassigned").astype(str).str.strip()
        working.loc[working[col] == "", col] = "Unassigned"
    return working


def get_top_pic(df: pd.DataFrame, pic_col: str, doc_col: str) -> str:
    if df.empty or pic_col not in df.columns or doc_col not in df.columns or "Status" not in df.columns:
        return "Tidak ada"

    working = assign_unassigned(df, pic_col)
    working = working[working[pic_col] != "Unassigned"]

    if working.empty:
        return "Tidak ada"

    # 🔹 Urutan prioritas status (semakin tinggi nilainya, semakin pending)
    status_priority = {
        "Need Approve": 4,
        "Approved": 3,
        "In Progress": 2,
        "Complete": 1
    }

    working["Status_Score"] = working["Status"].map(status_priority).fillna(0)

    summary = (
        working.groupby(pic_col)
        .agg(
            Total_Doc=(doc_col, "nunique"),
            Avg_Status_Score=("Status_Score", "mean")
        )
        .reset_index()
    )

    # 🔹 Urutkan berdasarkan jumlah dokumen dan tingkat pending (semakin tinggi skor, semakin pending)
    summary = summary.sort_values(["Total_Doc", "Avg_Status_Score"], ascending=[False, False])

    return summary.iloc[0][pic_col] if not summary.empty else "Tidak ada"


def summarize_status(df: pd.DataFrame, doc_col: str, nominal_col: str = "Nominal") -> pd.DataFrame:
    if df.empty or "Status" not in df.columns:
        return pd.DataFrame(columns=["Status", "Total_Doc", "Total_Amount"])

    working = df.copy()
    working = ensure_columns(working, [doc_col, nominal_col, "Status"])
    working = safe_to_numeric(working, [nominal_col])

    summary = (
        working.groupby("Status", dropna=False)
        .agg(
            Total_Doc=(doc_col, "nunique"),
            Total_Amount=(nominal_col, "sum")
        )
        .reset_index()
    )
    return summary

def summarize_pic_status(df: pd.DataFrame, pic_col: str, doc_col: str) -> pd.DataFrame:
    if df.empty or pic_col not in df.columns or "Status" not in df.columns or doc_col not in df.columns:
        return pd.DataFrame(columns=[pic_col, "Status", "Jumlah_Doc"])

    working = assign_unassigned(df, pic_col)

    summary = (
        working.groupby([pic_col, "Status"], dropna=False)
        .agg(Jumlah_Doc=(doc_col, "nunique"))
        .reset_index()
        .sort_values(by="Jumlah_Doc", ascending=False)
    )
    return summary

# =========================================================
# 8) CHART HELPERS
# =========================================================
STATUS_COLORS = {
    "Complete": "#00CC96",
    "In Progress": "#F2C94C",
    "Approved": "#F2994A",
    "Need Approve": "#EB5757",
    "Pending": "#56CCF2",
}

def render_status_pie(summary_df: pd.DataFrame, title: str):
    if summary_df.empty:
        st.info("Data status tidak tersedia.")
        return

    fig = px.pie(
        summary_df,
        values="Total_Amount",
        names="Status",
        color="Status",
        color_discrete_map=STATUS_COLORS,
        hole=0.45,
    )
    

    fig.update_traces(
        textinfo="percent+value",
        texttemplate="%{percent:.1%}<br>(Rp %{value:,.0f})"
    )
    st.plotly_chart(fig, use_container_width=True)


def render_status_bar(summary_df: pd.DataFrame, title: str):
    if summary_df.empty:
        st.info("Data status tidak tersedia.")
        return

    fig = px.bar(
        summary_df,
        x="Status",
        y="Total_Amount",
        color="Status",
        color_discrete_map=STATUS_COLORS,
        title=title
    )

    fig.update_traces(
        texttemplate="Rp %{y:,.0f}",
        textposition="outside"
    )
    fig.update_layout(
        showlegend=False,
        yaxis=dict(
            tickformat=",.0f",
            title="Total Nominal (Rp)"
        )
    )
    st.plotly_chart(fig, use_container_width=True)


def render_pic_bar(summary_df: pd.DataFrame, x_col: str, y_col: str, color_col: str | None):
    if summary_df.empty:
        st.info("Data PIC tidak tersedia.")
        return

    # Hitung total transaksi per PIC
    summary_df["Total_Doc"] = summary_df.groupby(x_col)[y_col].transform("sum")

    kwargs = {
        "data_frame": summary_df,
        "x": x_col,
        "y": y_col,
    }

    if color_col and color_col in summary_df.columns:
        kwargs["color"] = color_col
        kwargs["color_discrete_map"] = STATUS_COLORS

    fig = px.bar(**kwargs)

    # 🔹 Label per status (segmen warna) → di dalam bar
    fig.update_traces(
        texttemplate="%{y}",          # angka per status
        textposition="inside",
        textfont=dict(size=10, color="white")
    )

    # 🔹 Tambahkan angka total per PIC → di atas bar
    totals = summary_df.groupby(x_col)[y_col].sum().reset_index()
    for _, row in totals.iterrows():
        fig.add_annotation(
            x=row[x_col],             # posisi di sumbu X (PIC)
            y=row[y_col],             # tinggi bar total
            text=f"{row[y_col]}",     # angka total
            showarrow=False,
            font=dict(size=12, color="black"),
            yshift=10                 # geser sedikit ke atas
        )

    fig.update_layout(
        uniformtext_mode="hide",
        uniformtext_minsize=8,
    )

    st.plotly_chart(fig, use_container_width=True)


def render_pic_heatmap(df: pd.DataFrame, pic_col: str, date_col: str, doc_col: str, title: str):
    if df.empty or pic_col not in df.columns or date_col not in df.columns or doc_col not in df.columns:
        st.info("Data tidak tersedia untuk heatmap aktivitas PIC.")
        return

    working = df.copy()
    working[date_col] = pd.to_datetime(working[date_col], errors="coerce")
    working[pic_col] = working[pic_col].fillna("Unassigned")

    bulan_map = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
    working["Bulan"] = working[date_col].dt.month.map(bulan_map)
    bulan_order = list(bulan_map.values())
    working["Bulan"] = pd.Categorical(working["Bulan"], categories=bulan_order, ordered=True)

    # gunakan doc_col dinamis
    working[doc_col] = working[doc_col].astype(str).str.strip().str.upper()
    summary = (
        working.groupby([pic_col, "Bulan"])[doc_col]
        .nunique()
        .reset_index(name="Jumlah Transaksi")
        .sort_values("Bulan")
    )

    fig = px.density_heatmap(summary, x="Bulan", y=pic_col, z="Jumlah Transaksi",
                             color_continuous_scale=["#138207","#F2994A","#A80B0B"], text_auto=True)
    
    # tambahkan pengaturan layout di sini
    fig.update_layout(
        coloraxis_showscale=False,   # 🔹 sembunyikan color bar
        coloraxis_colorbar=dict(title=None),  # 🔹 hilangkan teks "sum of Jumlah Transaksi"
        xaxis_title="Bulan",
        yaxis_title="PIC Sales",
        margin=dict(l=100, r=40, t=60, b=120),
        height=500
        )
    st.plotly_chart(fig, use_container_width=True)


    # Tambahkan keterangan di bawah heatmap
    st.markdown(
        "<div style='text-align:center; font-size:0.8rem; color:#6f6f6f;'>"
        "📝 <b>Keterangan:</b> " \
        "Kotak dengan warna mendekati merah artinya punya outstanding PR yang lebih banyak sedangkan " \
        "kotak dengan warna mendekati biru artinya outstanding PRnya lebih sedikit"
        "</div>",
        unsafe_allow_html=True
    )


def build_customer_pareto(
    df: pd.DataFrame,
    customer_col: str = "Customer",
    revenue_col: str = "total_si_row",
    transaction_col: str = "transaction_number_si",
    threshold: float = 0.80,
):
    output_columns = [
        "Rank",
        "Customer",
        "Total_SI",
        "Revenue",
        "Kontribusi",
        "Kumulatif",
        "Kategori",
    ]

    if df.empty or customer_col not in df.columns:
        return pd.DataFrame(columns=output_columns)

    working = df.copy()

    working[customer_col] = (
        working[customer_col]
        .fillna("Customer Tidak Diketahui")
        .astype(str)
        .str.strip()
    )

    working.loc[
        working[customer_col].eq(""),
        customer_col
    ] = "Customer Tidak Diketahui"

    working[revenue_col] = pd.to_numeric(
        working[revenue_col],
        errors="coerce"
    ).fillna(0)

    # Abaikan nilai nol atau negatif dari perhitungan Pareto
    working = working[working[revenue_col] > 0].copy()

    if working.empty:
        return pd.DataFrame(columns=output_columns)

    pareto = (
        working.groupby(customer_col, as_index=False)
        .agg(
            Total_SI=(transaction_col, "nunique"),
            Revenue=(revenue_col, "sum"),
        )
        .rename(columns={customer_col: "Customer"})
        .sort_values("Revenue", ascending=False)
        .reset_index(drop=True)
    )

    total_revenue = pareto["Revenue"].sum()

    if total_revenue <= 0:
        return pd.DataFrame(columns=output_columns)

    pareto["Kontribusi"] = pareto["Revenue"] / total_revenue
    pareto["Kumulatif"] = pareto["Kontribusi"].cumsum()
    pareto["Rank"] = range(1, len(pareto) + 1)

    # Customer yang membuat kumulatif melewati 80% tetap dimasukkan
    jumlah_pareto = (
        pareto["Kumulatif"]
        .searchsorted(threshold, side="left") + 1
    )

    pareto["Kategori"] = "Non-Pareto"
    pareto.loc[
        pareto.index < jumlah_pareto,
        "Kategori"
    ] = "Pareto 80%"

    return pareto[output_columns]


def build_customer_concentration(
    df: pd.DataFrame,
    customer_col: str = "Customer",
    revenue_col: str = "total_si_row",
    transaction_col: str = "transaction_number_si",
) -> tuple[pd.DataFrame, dict]:
    required = {customer_col, revenue_col, transaction_col}

    if df.empty or not required.issubset(df.columns):
        return pd.DataFrame(), {
            "CR1": 0,
            "CR5": 0,
            "CR10": 0,
            "HHI": 0,
            "Total_Customer": 0,
        }

    working = df.copy()

    working[customer_col] = (
        working[customer_col]
        .fillna("Customer Tidak Diketahui")
        .astype(str)
        .str.strip()
    )

    working[revenue_col] = pd.to_numeric(
        working[revenue_col],
        errors="coerce",
    ).fillna(0)

    working = working[working[revenue_col] > 0].copy()

    if working.empty:
        return pd.DataFrame(), {
            "CR1": 0,
            "CR5": 0,
            "CR10": 0,
            "HHI": 0,
            "Total_Customer": 0,
        }

    summary = (
        working.groupby(customer_col, as_index=False)
        .agg(
            Revenue=(revenue_col, "sum"),
            Total_SI=(transaction_col, "nunique"),
        )
        .rename(columns={customer_col: "Customer"})
        .sort_values("Revenue", ascending=False)
        .reset_index(drop=True)
    )

    total_revenue = summary["Revenue"].sum()

    summary["Share"] = summary["Revenue"] / total_revenue
    summary["Cumulative_Share"] = summary["Share"].cumsum()
    summary["Rank"] = range(1, len(summary) + 1)

    # HHI menggunakan skala 0–10.000
    hhi = (summary["Share"].pow(2).sum()) * 10_000

    metrics = {
        "CR1": summary.head(1)["Share"].sum(),
        "CR5": summary.head(5)["Share"].sum(),
        "CR10": summary.head(10)["Share"].sum(),
        "HHI": hhi,
        "Total_Customer": summary["Customer"].nunique(),
    }

    return summary, metrics

def build_monthly_customer_retention(
    df: pd.DataFrame,
    customer_col: str = "Customer",
    date_col: str = "transaction_date",
    transaction_col: str = "transaction_number_si",
    revenue_col: str = "total_si_row",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = {
        customer_col,
        date_col,
        transaction_col,
        revenue_col,
    }

    if df.empty or not required.issubset(df.columns):
        return pd.DataFrame(), pd.DataFrame()

    working = df.copy()

    working[customer_col] = (
        working[customer_col]
        .fillna("Customer Tidak Diketahui")
        .astype(str)
        .str.strip()
    )

    working[date_col] = pd.to_datetime(
        working[date_col],
        errors="coerce",
    )

    working[revenue_col] = pd.to_numeric(
        working[revenue_col],
        errors="coerce",
    ).fillna(0)

    working = working[
        working[date_col].notna()
        & working[customer_col].ne("")
        & working[customer_col].ne("Customer Tidak Diketahui")
    ].copy()

    if working.empty:
        return pd.DataFrame(), pd.DataFrame()

    working["Bulan"] = working[date_col].dt.to_period("M")

    # Satu baris per customer per bulan
    customer_month = (
        working.groupby([customer_col, "Bulan"], as_index=False)
        .agg(
            Revenue=(revenue_col, "sum"),
            Total_SI=(transaction_col, "nunique"),
        )
    )

    first_month = (
        customer_month.groupby(customer_col)["Bulan"]
        .min()
        .rename("Bulan_Pertama")
        .reset_index()
    )

    customer_month = customer_month.merge(
        first_month,
        on=customer_col,
        how="left",
    )

    customer_month["Tipe_Customer"] = customer_month.apply(
        lambda row: (
            "Customer Baru"
            if row["Bulan"] == row["Bulan_Pertama"]
            else "Customer Existing"
        ),
        axis=1,
    )

    # Periksa apakah customer aktif pada bulan sebelumnya
    previous_activity = customer_month[[customer_col, "Bulan"]].copy()
    previous_activity["Bulan"] = previous_activity["Bulan"] + 1
    previous_activity["Aktif_Bulan_Sebelumnya"] = True

    customer_month = customer_month.merge(
        previous_activity,
        on=[customer_col, "Bulan"],
        how="left",
    )

    customer_month["Retained"] = (
        customer_month["Aktif_Bulan_Sebelumnya"]
        .fillna(False)
        .astype(bool)
    )

    monthly = (
        customer_month.groupby("Bulan", as_index=False)
        .agg(
            Active_Customers=(customer_col, "nunique"),
            New_Customers=(
                "Tipe_Customer",
                lambda values: (values == "Customer Baru").sum(),
            ),
            Returning_Customers=(
                "Tipe_Customer",
                lambda values: (values == "Customer Existing").sum(),
            ),
            Retained_Customers=("Retained", "sum"),
            Revenue=("Revenue", "sum"),
        )
        .sort_values("Bulan")
    )

    monthly["Previous_Active_Customers"] = (
        monthly["Active_Customers"].shift(1)
    )

    monthly["Retention_Rate"] = (
        monthly["Retained_Customers"]
        / monthly["Previous_Active_Customers"]
    )

    monthly["Bulan_Label"] = monthly["Bulan"].astype(str)

    return monthly, customer_month


# =========================================================
# 9) MAIN APP
# =========================================================

def main():
    st.title("SIBIMA Performance Dashboard - SALES")

    # ---------- TOP FILTERS ----------
    today = date.today()
    default_start = date(today.year, today.month, 1)

    col_head1, col_head2, col_head3, col_head4, col_head5 = st.columns([1, 1, 1, 1, 1])

    with col_head1:
        selected_date_range = st.date_input(
            "Select Date Range 📅",
            value=(default_start, today),
            max_value=today
        )

    with col_head2:
        selected_doc_type = st.selectbox("Pilih Jenis Dokumen 📑", ["SO", "DO", "NPR", "PUR"])

    with col_head3:
        search_number = st.text_input("Cari Nomor Transaksi 🔍", placeholder="No. SO")

    with col_head4:
        search_status = st.text_input("Cari Status 🔍", placeholder="Complete / In Progress / Approved / Need Approve")

    # ---------- LOAD DATA ----------
    if isinstance(selected_date_range, (tuple, list)) and len(selected_date_range) == 2:
        start_date, end_date = selected_date_range
    else:
        start_date, end_date = default_start, today

    with st.spinner("Mengambil data dashboard..."):
        data_old = load_all_data()
        data_new = load_all_data_new(start_date=start_date, end_date=end_date)

        # SO Balance tidak lagi memakai endpoint so-balance lama.
        # Dibangun ulang dari raw SO + DO dengan business rule:
        # NO DO atau PARTIAL DO saja.
        balance_source_api = load_so_balance_source_api(
            start_date=SO_BALANCE_BASE_START_DATE,
            end_date=end_date,
        )
        data_old["so"] = _build_so_balance_view(
            balance_source_api.get("so", pd.DataFrame()),
            balance_source_api.get("do", pd.DataFrame()),
        )

    # ---------- ASSIGN DATAFRAME ----------
    df_so = data_old["so"]
    df_pr = data_old["pr"]
    df_po = data_old["po"]
    df_grn = data_old["grn"]
    df_do = data_old["do"]
    df_npr = data_old["npr"]
    #df_pur = data_old["pur"]

    df_so_final = data_new["so"]
    df_pr_final = data_new["pr"]
    df_po_final = data_new["po"]
    df_grn_final = data_new["grn"]
    df_do_final = data_new["do"]
    df_si_final = data_new["si"]
    #df_npr_final = data_new["npr"]

    # Pastikan kolom PIC dan Status sesuai
    #SO
    df_so_final = df_so_final.rename(columns={
        #"item_pic_procurement_name": "PIC Procurement",
        "status_description": "Status_so",
        "pic_sales_name" : "PIC Sales",
        "item_id": "so_detail_id",
        "transaction_number" : "transaction_number_so",
        "item_product_id" : "product_id",
        "item_item_name" : "item_name",
        "customer_name": "Customer",
    })

    # =========================================================
    # NORMALISASI STATUS SO SEDINI MUNGKIN
    # =========================================================
    # Disamakan dengan behavior versi PostgreSQL: status sudah
    # dinormalisasi sebelum search filter dijalankan. Dengan ini
    # status numerik/alias seperti 2, 3, 4, completed, approved1,
    # dst. terlebih dahulu menjadi vocabulary dashboard standar.
    if "Status_so" in df_so_final.columns:
        df_so_final["Status_so"] = (
            df_so_final["Status_so"]
            .map(normalize_api_status)
            .fillna("")
            .astype(str)
            .str.strip()
        )
    #PR
    df_pr_final = df_pr_final.rename(columns={
        "item_pic_procurement_name": "PIC Procurement",
        "status_description": "Status_pr",
        "item_id": "pr_detail_id",
        "item_so_detail_id" : "so_detail_id",
        "transaction_number" : "transaction_number_pr",
        "item_product_id" : "product_id"
    })
    #PO
    df_po_final = df_po_final.rename(columns={
        "item_pic_procurement_name": "PIC Procurement",
        "status_description": "Status_po",
        "item_id": "po_detail_id",
        "item_pr_detail_id" : "pr_detail_id",
        "transaction_number" : "transaction_number_po",
        "item_product_id" : "product_id"
    })
    #GRN
    df_grn_final = df_grn_final.rename(columns={
        "item_pic_procurement_name": "PIC Procurement",
        "status_description": "Status_grn",
        "item_id": "grn_detail_id",
        "item_po_detail_id" : "po_detail_id",
        "transaction_number" : "transaction_number_grn",
        "item_product_id" : "product_id"
    })
    #DO
    df_do_final = df_do_final.rename(columns={
        "item_pic_procurement_name": "PIC Procurement",
        "status_description": "Status_do",
        "item_id": "do_detail_id",
        "item_grn_detail_id" : "grn_detail_id",
        "transaction_number" : "transaction_number_do",
        "item_product_id" : "product_id",
        "item_so_detail_id": "so_detail_id",
    })

    #SI
    df_si_final = df_si_final.rename(columns={
        "status_description": "Status_si",
        "item_do_detail_id": "do_detail_id",
        "item_id": "si_detail_id",
        "transaction_number": "transaction_number_si",
        "item_product_id": "product_id",
        "customer_name": "Customer",
        "item_item_name": "item_name"
})

    df_do = df_do.rename(columns={
        "Status DO": "Status",
    })

    # Pastikan kolom tanggal sudah dalam format datetime
    #SO
    df_so_final = safe_to_datetime(df_so_final, "transaction_date")
    df_so_final = safe_to_datetime(df_so_final, "date_approved")
    df_so_final = safe_to_datetime(df_so_final, "date_inprogress")
    df_so_final = safe_to_datetime(df_so_final, "date_complete")
    #PR
    df_pr_final = safe_to_datetime(df_pr_final, "transaction_date")
    df_pr_final = safe_to_datetime(df_pr_final, "date_approved")
    df_pr_final = safe_to_datetime(df_pr_final, "date_inprogress")
    df_pr_final = safe_to_datetime(df_pr_final, "date_complete")
    #DO
    df_do_final = safe_to_datetime(df_do_final, "transaction_date")
    df_do_final = safe_to_datetime(df_do_final, "date_approved")
    df_do_final = safe_to_datetime(df_do_final, "date_inprogress")
    df_do_final = safe_to_datetime(df_do_final, "date_complete")
    #NPR
    #df_npr_final = safe_to_datetime(df_npr_final, "transaction_date")
    #df_npr_final = safe_to_datetime(df_npr_final, "date_approved")
    #df_npr_final = safe_to_datetime(df_npr_final, "date_inprogress")
    #df_npr_final = safe_to_datetime(df_npr_final, "date_complete")
    #SI
    df_si_final = safe_to_datetime(df_si_final, "transaction_date")
    df_si_final = safe_to_datetime(df_si_final, "date_approved")
    df_si_final = safe_to_datetime(df_si_final, "date_inprogress")
    df_si_final = safe_to_datetime(df_si_final, "date_complete")

    # Snapshot raw SO source untuk audit Total SO.
    # PostgreSQL: dapat memuat histori lebih luas; API: sesuai response endpoint.
    debug_so_raw_source = df_so_final.copy()

        # ---------- EXTRACT UNIQUE PIC LIST ----------
    # Ambil list PIC Procurement unik dari df_pr_final (dan dataframe lain jika perlu)
    pic_list = []
    if "PIC Sales" in df_so_final.columns:
        pic_list = df_so_final["PIC Sales"].dropna().astype(str).str.strip()
        pic_list = [pic for pic in pic_list.unique() if pic != "" and pic.lower() != "nan"]
        pic_list.sort()

    # Tambahkan opsi 'Semua PIC' di urutan pertama
    pic_options = ["Semua PIC"] + pic_list

    # ---------- TOP FILTERS (Tahap 2: Dropdown PIC) ----------
    with col_head5:
        search_pic = st.selectbox(
            "Pilih PIC Sales 👤",
            options=pic_options,
            index=0
        )

    # ---------- DEFAULT SAFE COPY ----------
    df_so_f = df_so.copy()
    df_pr_f = df_pr.copy()
    df_po_f = df_po.copy()
    df_grn_f = df_grn.copy()
    df_do_f = df_do.copy()
    df_npr_f = df_npr.copy()
    #df_pur_f = df_pur.copy()
    df_so_final_f = df_so_final.copy()
    df_pr_final_f = df_pr_final.copy()
    df_po_final_f = df_po_final.copy()
    df_grn_final_f = df_grn_final.copy()
    df_do_final_f = df_do_final.copy()
    df_si_final_f = df_si_final.copy()
    #df_npr_final_f = df_pr_final.copy()

    # ---------- DATE FILTER ----------
    if isinstance(selected_date_range, (tuple, list)) and len(selected_date_range) == 2:
        report_start_date, report_end_date = selected_date_range
        df_so_final_f = apply_cumulative_filter(df_so_final_f, report_end_date)
        df_pr_final_f = apply_cumulative_filter(df_pr_final_f, report_end_date)
        df_po_final_f = apply_cumulative_filter(df_po_final_f, report_end_date)
        df_grn_final_f = apply_cumulative_filter(df_grn_final_f, report_end_date)
        df_do_final_f = apply_cumulative_filter(df_do_final_f, report_end_date)
        df_si_final_f = apply_cumulative_filter(df_si_final_f, report_end_date)
        #df_npr_final_f = apply_cumulative_filter(df_npr_final_f, report_end_date)



        # =====================================================
        # TOTAL SO: IKUTI PERIODE YANG DIPILIH USER
        # =====================================================
        # Contoh: bila user memilih 1 Sep 2026 s/d 23 Sep 2026,
        # maka hanya SO dengan transaction_date dalam periode tersebut
        # yang masuk ke dataset Total SO.
        df_so_final_real = apply_realization_filter(
            df_so_final,
            report_start_date,
            report_end_date
        )

        debug_so_period_filtered = df_so_final_real.copy()

        # Dataset lain (PR, PO, GRN, DO, SI) tetap cumulative sampai end date
        df_pr_final_real = apply_cumulative_filter(df_pr_final, report_end_date)
        df_po_final_real = apply_cumulative_filter(df_po_final, report_end_date)
        df_grn_final_real = apply_cumulative_filter(df_grn_final, report_end_date)
        df_do_final_real = apply_cumulative_filter(df_do_final, report_end_date)
        df_si_final_real = apply_cumulative_filter(df_si_final, report_end_date)
        # Metric Revenue/Pareto mengikuti date range yang dipilih.
        df_si_metric_real = apply_realization_filter(
            df_si_final, report_start_date, report_end_date
        )

    # ---------- SEARCH FILTER ----------
    df_so_final_f = apply_search_filter(df_so_final_f, search_number, search_status, search_pic)
    df_pr_final_f = apply_search_filter(df_pr_final_f, search_number, search_status, search_pic)
    df_so_final_real = apply_search_filter(df_so_final_real, search_number, search_status, search_pic)
    debug_so_after_search = df_so_final_real.copy()
    df_so_f = apply_search_filter(df_so_f,search_number,search_status,search_pic)
    #df_po_f = apply_search_filter(df_po_f, search_number, search_status, search_pic)
    #df_grn_f = apply_search_filter(df_grn_f, search_number, search_status, search_pic)
    #df_do_f = apply_search_filter(df_do_f, search_number, search_status, search_pic)
    #df_npr_f = apply_search_filter(df_npr_f, search_number, search_status, search_pic)
    #df_pur_f = apply_search_filter(df_pur_f, search_number, search_status, search_pic)
    #df_pr_final_real = apply_search_filter(df_pr_final_real, search_number, search_status, search_pic)


    #df_pur_f = ensure_columns(df_pur_f, ["No. PUR", "PIC", "Status"])
    df_so_final_real = ensure_columns(df_so_final_real, ["so_detail_id", "transaction_number_so","Status_so", "product_id", "item_name"])
    df_pr_final_real = ensure_columns(df_pr_final_real, ["pr_detail_id", "so_detail_id", "transaction_number_pr", "product_id"])
    df_po_final_real = ensure_columns(df_po_final_real, ["po_detail_id", "pr_detail_id", "transaction_number_po", "product_id"])
    df_grn_final_real = ensure_columns(df_grn_final_real, ["po_detail_id", "grn_detail_id", "transaction_number_grn", "product_id"])
    df_do_final_real = ensure_columns(df_do_final_real, ["so_detail_id", "grn_detail_id", "do_detail_id", "transaction_number_do", "product_id"])
    df_si_final_real = ensure_columns(df_si_final_real, ["do_detail_id", "si_detail_id", "transaction_number_si", "product_id"])
    df_si_metric_real = ensure_columns(df_si_metric_real, ["do_detail_id", "si_detail_id", "transaction_number_si", "product_id", "Status_si", "item_name"])

    df_so_f = safe_to_numeric(df_so_f, ["Nominal"])
    df_pr_f = safe_to_numeric(df_pr_f, ["Nominal"])
    df_po_f = safe_to_numeric(df_po_f, ["Nominal"])
    df_grn_f = safe_to_numeric(df_grn_f, ["Nominal"])
    df_do_f = safe_to_numeric(df_do_f, ["Nominal"])
    #df_pr_final_real = safe_to_numeric(df_pr_final_real, ["price", "discount", "quantity", "tax1_percentage", "tax2_percentage"])
    df_so_final_real= safe_to_numeric(df_so_final_real, ["item_price", "item_discount", "item_quantity", "item_tax1_percentage", "item_tax2_percentage"])
    df_pr_final_real= safe_to_numeric(df_pr_final_real, ["item_price", "item_discount", "item_quantity", "item_tax1_percentage", "item_tax2_percentage"])
    df_do_final_real= safe_to_numeric(df_do_final_real, ["item_price", "item_discount", "item_quantity", "item_tax1_percentage", "item_tax2_percentage"])
    df_si_final_real= safe_to_numeric(df_si_final_real, ["item_price", "item_discount", "item_quantity", "item_tax1_percentage", "item_tax2_percentage"])
    df_si_metric_real= safe_to_numeric(df_si_metric_real, ["item_price", "item_discount", "item_quantity", "item_tax1_percentage", "item_tax2_percentage"])

        # ---------- METRICS ----------
    total_so_unpr = safe_sum(df_so_f, "Nominal")
    total_pr_unpr = safe_sum(df_pr_f, "Nominal")
    total_po_unpr = safe_sum(df_po_f, "Nominal")
    total_grn_unpr = safe_sum(df_grn_f, "Nominal")
    total_do_unpr = safe_sum(df_do_f, "Nominal")
    #total_pr = safe_sum(df_pr_final_real, "transaction_total")

    df_so_final_real = normalize_text_columns(df_so_final_real, ["item_PIC_Procurement"])
    df_pr_final_real = normalize_text_columns(df_pr_final_real, ["item_PIC_Procurement"])
    df_do_final_real = normalize_text_columns(df_do_final_real, ["item_PIC_Procurement"])


    df_so_final_real["disc_per_unit"] = df_so_final_real["item_price"] * (df_so_final_real["item_discount"] / 100)
    df_so_final_real["tax_unit"] = (df_so_final_real["item_price"] - df_so_final_real["disc_per_unit"]) * (df_so_final_real["item_tax1_percentage"] / 100)
    df_so_final_real["net_price_unit"] = df_so_final_real["item_price"] - df_so_final_real["disc_per_unit"] + df_so_final_real["tax_unit"]
    df_so_final_real["nominal_so"] = df_so_final_real["item_quantity"] * df_so_final_real["net_price_unit"]


    df_so_final_real["Status_so"] = (
        df_so_final_real["Status_so"]
        .map(normalize_api_status)
        .fillna("")
        .astype(str)
        .str.strip()
    )
    #df_so_total = df_so_final_real[
    #~df_so_final_real["Status"].isin(["Draft"])
    #].copy()
    status_filter = ['In Progress', 'Approved', 'Complete']
    df_so_total = df_so_final_real[df_so_final_real['Status_so'].isin(status_filter)]
    debug_so_status_valid = df_so_total.copy()
    keyword_to_exclude = ['Jasa', 'Biaya', 'Admin', 'Pengiriman']
    pattern = '|'.join([re.escape(word) for word in keyword_to_exclude])

    # Filter Keyword Tahap  (Hanya untuk revenue) ---
    so_total_keyword = [df_so_total]
    processed_keyword_so_total = []

    for df in so_total_keyword :
        if 'item_name' in df.columns:
            df = df[~df['item_name'].astype(str).str.contains(pattern, case=False, na=False)]
        processed_keyword_so_total.append(df)

    # PERBAIKAN: Ambil elemen pertama dari list, jangan simpan list-nya ke variabel df_so_f
    df_so_total = processed_keyword_so_total[0] 

    df_so_total["disc_per_unit"] = df_so_total["item_price"] * (df_so_total["item_discount"] / 100)
    df_so_total["tax_unit"] = (df_so_total["item_price"] - df_so_total["disc_per_unit"]) * (df_so_total["item_tax1_percentage"] / 100)
    df_so_total["net_price_unit"] = df_so_total["item_price"] - df_so_total["disc_per_unit"] + df_so_total["tax_unit"]
    df_so_total["total_so_row"] = df_so_total["item_quantity"] * df_so_total["net_price_unit"]
    total_so = df_so_total["total_so_row"].sum()

    debug_so_final_total = df_so_total.copy()

    debug_total_so_snapshots = {
        "RAW_SOURCE": debug_so_raw_source,
        "PERIOD_FILTERED": debug_so_period_filtered,
        "AFTER_SEARCH_FILTER": debug_so_after_search,
        "STATUS_VALID": debug_so_status_valid,
        "FINAL_TOTAL_SO": debug_so_final_total,
    }
    debug_total_so_summary, debug_total_so_excel, debug_total_so_gap = (
        build_total_so_debug_package(
            source_label="ERP API",
            snapshots=debug_total_so_snapshots,
            app_total_so=total_so,
        )
    )



    debug_revenue_raw_source = df_si_final.copy()
    debug_revenue_period_filtered = df_si_metric_real.copy()

    df_si_metric_real["Status_si"] = (
        df_si_metric_real["Status_si"]
        .map(normalize_api_status)
        .fillna("")
        .astype(str)
        .str.strip()
    )
    status_filter2 = ['In Progress', 'Approved', 'Complete', 'Draft' ]
    df_si_status_valid = df_si_metric_real[df_si_metric_real['Status_si'].isin(status_filter2)].copy()
    debug_revenue_status_valid = df_si_status_valid.copy()
    df_si_total = df_si_status_valid.copy()
    keyword_to_exclude2 = ['Jasa', 'Biaya', 'Admin', 'Pengiriman']
    pattern2 = '|'.join([re.escape(word) for word in keyword_to_exclude2])

    # Filter Keyword Tahap  (Hanya untuk revenue) ---
    si_total_keyword = [df_si_total]
    processed_keyword_si_total = []

    for df in si_total_keyword :
        if 'item_name' in df.columns:
            df = df[~df['item_name'].astype(str).str.contains(pattern2, case=False, na=False)]
        processed_keyword_si_total.append(df)

    # PERBAIKAN: Ambil elemen pertama dari list, jangan simpan list-nya ke variabel df_so_f
    df_si_total = processed_keyword_si_total[0] 

    df_si_total["disc_per_unit"] = df_si_total["item_price"] * (df_si_total["item_discount"] / 100)
    df_si_total["tax_unit"] = (df_si_total["item_price"] - df_si_total["disc_per_unit"]) * (df_si_total["item_tax1_percentage"] / 100)
    df_si_total["net_price_unit"] = df_si_total["item_price"] - df_si_total["disc_per_unit"] + df_si_total["tax_unit"]
    df_si_total["total_si_row"] = df_si_total["item_quantity"] * df_si_total["net_price_unit"]
    total_si = df_si_total["total_si_row"].sum()

    debug_revenue_final = df_si_total.copy()
    revenue_debug_snapshots = {
        "RAW_SOURCE": debug_revenue_raw_source,
        "PERIOD_FILTERED": debug_revenue_period_filtered,
        "STATUS_VALID": debug_revenue_status_valid,
        "FINAL_REVENUE": debug_revenue_final,
    }
    debug_revenue_summary, debug_revenue_excel, debug_revenue_gap = build_revenue_debug_package(
        "ERP API", revenue_debug_snapshots, float(total_si)
    )


    df_pr_final_real["disc_per_unit"] = df_pr_final_real["item_price"] * (df_pr_final_real["item_discount"] / 100)
    df_pr_final_real["tax_unit"] = (df_pr_final_real["item_price"] - df_pr_final_real["disc_per_unit"]) * (df_pr_final_real["item_tax1_percentage"] / 100)
    df_pr_final_real["net_price_unit"] = df_pr_final_real["item_price"] - df_pr_final_real["disc_per_unit"] + df_pr_final_real["tax_unit"]
    df_pr_final_real["total_pr_row"] = df_pr_final_real["item_quantity"] * df_pr_final_real["net_price_unit"]
    total_pr = df_pr_final_real["total_pr_row"].sum()

    df_do_final_real["disc_per_unit"] = df_do_final_real["item_price"] * (df_do_final_real["item_discount"] / 100)
    df_do_final_real["tax_unit"] = (df_do_final_real["item_price"] - df_do_final_real["disc_per_unit"]) * (df_do_final_real["item_tax1_percentage"] / 100)
    df_do_final_real["net_price_unit"] = df_do_final_real["item_price"] - df_do_final_real["disc_per_unit"] + df_do_final_real["tax_unit"]
    df_do_final_real["total_do_row"] = df_do_final_real["item_quantity"] * df_do_final_real["net_price_unit"]
    total_do = df_do_final_real["total_do_row"].sum()

    #df_npr_final_real["disc_per_unit"] = df_npr_final_real["item_price"] * (df_npr_final_real["item_discount"] / 100)
    #df_npr_final_real["tax_unit"] = (df_npr_final_real["item_price"] - df_npr_final_real["disc_per_unit"]) * (df_npr_final_real["item_tax1_percentage"] / 100)
    #df_npr_final_real["net_price_unit"] = df_npr_final_real["item_price"] - df_npr_final_real["disc_per_unit"] + df_npr_final_real["tax_unit"]
    #df_npr_final_real["total_pr_row"] = df_npr_final_real["item_quantity"] * df_npr_final_real["net_price_unit"]
    #total_npr = df_npr_final_real["total_pr_row"].sum()

    #df_do_final_real["disc_per_unit"] = df_do_final_real["item_price"] * (df_do_final_real["item_discount"] / 100)
    #df_do_final_real["tax_unit"] = (df_do_final_real["item_price"] - df_do_final_real["disc_per_unit"]) * (df_do_final_real["item_tax1_percentage"] / 100)
    #df_do_final_real["tax_unit"] = df_do_final_real["item_tax1_value"] + df_do_final_real["item_tax1_value"]
    #df_do_final_real["net_price_unit"] = df_do_final_real["item_price"] - df_do_final_real["disc_per_unit"] + df_do_final_real["tax_unit"]
    #df_do_final_real["net_price_unit"] = df_do_final_real["item_price"] - df_do_final_real["disc_per_unit"]
    #df_do_final_real["total_do_row"] = df_do_final_real["item_quantity"] * df_do_final_real["net_price_unit"]
    
    total_so_count = safe_unique_count(df_so_total, "transaction_number_so")
    total_so_balance_count = safe_unique_count(df_so_f, "No. SO")
    total_so_rows = len(df_so_total)
    total_so_balance_rows = len(df_so_f)

    total_pr_count = safe_unique_count(df_pr_final_real, "transaction_number")
    total_pr_balance_count = safe_unique_count(df_pr_f, "No. PR")
    total_pr_rows = len(df_pr_final_real)
    total_pr_balance_rows = len(df_pr_f)
    total_do_count = safe_unique_count(df_do_final_real, "transaction_number")
    total_do_balance_count = safe_unique_count(df_do_f, "No. DO")
    total_do_rows = len(df_do_final_real)
    total_do_balance_rows = len(df_do_f)
    #total_npr_count = safe_unique_count(df_npr_f, "No. Transaksi")
    #total_npr_rows = len(df_npr_f)

    avg_nominal_so = safe_mean(df_so_f, "Nominal")
    avg_nominal_do = safe_mean(df_do_f, "Nominal")

    top_pic_so = get_top_pic(df_so_f, "PIC Sales", "No. SO")
    top_pic_pr = get_top_pic(df_pr_f, "PIC Procurement", "No. PR")
    top_pic_do = get_top_pic(df_do_f, "PIC Procurement", "No. DO")
    #top_pic_pur = get_top_pic(df_pur_f, "PIC", "No. PUR")

    debug_so_balance_summary, debug_so_balance_status, debug_so_balance_excel = (
        build_so_balance_debug_package("ERP API", df_so, df_so_f)
    )
    debug_pic_excel = build_pic_debug_excel_api(df_so_final_real)

    df_customer_concentration, concentration_metrics = (
    build_customer_concentration(
        df_si_total,
        customer_col="Customer",
        revenue_col="total_si_row",
        transaction_col="transaction_number_si",
    )
)

    df_monthly_retention, df_customer_month = (
    build_monthly_customer_retention(
        df_si_total,
        customer_col="Customer",
        date_col="transaction_date",
        transaction_col="transaction_number_si",
        revenue_col="total_si_row",
    )
)

    # ---------- LAYOUT ----------
    #col_kiri, col_tengah, col_kanan = st.columns([1, 1, 1], gap="small")
    col_kiri, col_tengah = st.columns([1, 1], gap="small")


    # Konversi semua kolom ID menjadi integer murni
    for col in [
        "so_detail_id", "pr_detail_id", "po_detail_id",
        "grn_detail_id", "do_detail_id"
    ]:
        for df in [
            df_so_final_real, df_pr_final_real, df_po_final_real,
            df_grn_final_real, df_do_final_real, df_si_final_real
        ]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")


    #so_pr = df_so_final_real.merge(df_pr_final_real, left_on='detail_id', right_on='so_detail_id', how='outer')
    #pr_po = so_pr.merge(df_po_final_real, left_on='pr_detail_id', right_on='pr_detail_id', how='outer')
    #po_grn = pr_po.merge(df_grn_final_real, left_on='po_detail_id', right_on='po_detail_id', how='outer')
    #grn_do = po_grn.merge(df_do_final_real, left_on='grn_detail_id', right_on='grn_detail_id', how='outer')
    #final_merge = grn_do.merge(df_si_final_real, left_on='do_detail_id', right_on='do_detail_id', how='outer')


    # Set Subset (Sertakan transaction_date dan beri nama yang spesifik)
    df_so_subset = df_so_total[[
        "so_detail_id", "transaction_number_so", "transaction_date", "Status_so","product_id","item_name", "PIC Sales", "item_price", "item_quantity", "item_discount",
    "item_tax1_percentage","nominal_so",
    ]].rename(columns={
        "transaction_date": "transaction_date_so"
    })

    df_pr_subset = df_pr_final_real[[
        "so_detail_id", "pr_detail_id", "transaction_number_pr", "transaction_date", "Status_pr", "product_id", "PIC Procurement"
    ]].rename(columns={"transaction_date": "transaction_date_pr"})

    df_po_subset = df_po_final_real[[
        "pr_detail_id", "po_detail_id", "transaction_number_po", "transaction_date", "Status_po", "product_id"
    ]].rename(columns={"transaction_date": "transaction_date_po"})

    df_grn_subset = df_grn_final_real[[
        "po_detail_id", "grn_detail_id", "transaction_number_grn", "transaction_date", "Status_grn", "product_id", "vendor_name"
    ]].rename(columns={"transaction_date": "transaction_date_grn"})

    df_do_subset = df_do_final_real[[
        "so_detail_id", "grn_detail_id", "do_detail_id", "transaction_number_do", "transaction_date", "Status_do", "product_id"
    ]].rename(columns={"transaction_date": "transaction_date_do"})

    df_si_subset = df_si_final_real[[
        "do_detail_id", "si_detail_id", "transaction_number_si", "transaction_date", "Status_si", "product_id"
    ]].rename(columns={"transaction_date": "transaction_date_si"})

    # 1. Merge SO ke PR
    # Agar lebih presisi, kita gunakan merge berbasis so_detail_id & product_id
    so_pr = df_so_subset.merge(
        df_pr_subset[df_pr_subset["so_detail_id"].notna()],
        how="left",
        on=["so_detail_id", "product_id"],
        suffixes=("", "_pr")
    )

    # 2. Merge PR ke PO
    pr_po = so_pr.merge(
        df_po_subset[df_po_subset["pr_detail_id"].notna()],
        how="left",
        on=["pr_detail_id", "product_id"],
        suffixes=("", "_po")
    )

    # 3. Merge PO ke GRN
    po_grn = pr_po.merge(
        df_grn_subset[df_grn_subset["po_detail_id"].notna()],
        how="left",
        on=["po_detail_id", "product_id"],
        suffixes=("", "_grn")
    )

    # 4. JALUR A: Join GRN -> DO (Hanya jika grn_detail_id ada)
    po_grn_do_via_grn = po_grn.merge(
        df_do_subset[df_do_subset["grn_detail_id"].notna()].drop(columns=["so_detail_id"], errors="ignore"),
        how="left",
        on=["grn_detail_id", "product_id"],
        suffixes=("", "_do_grn")
    )

    # 5. JALUR B: Join SO -> DO Direct (Hanya jika DO tersebut punya so_detail_id)
    df_do_direct_so = df_do_subset[df_do_subset["so_detail_id"].notna()].copy()
    
    final_do_step = po_grn_do_via_grn.merge(
        df_do_direct_so,
        how="left",
        on=["so_detail_id", "product_id"],
        suffixes=("", "_direct_so")
    )

    # 6. COALESCE: Jika do_detail_id dari GRN kosong, isi dari Direct SO
    for col_base in ["do_detail_id", "transaction_number_do", "Status_do", "transaction_date_do"]:
        col_direct = f"{col_base}_direct_so"
        if col_direct in final_do_step.columns:
            final_do_step[col_base] = final_do_step[col_base].fillna(final_do_step[col_direct])
            final_do_step.drop(columns=[col_direct], inplace=True)

    # Bersihkan kolom duplikat grn_detail_id dari direct_so jika ada
    if "grn_detail_id_direct_so" in final_do_step.columns:
        final_do_step.drop(columns=["grn_detail_id_direct_so"], inplace=True)

    # 7. Join DO -> SI (Hanya jika do_detail_id ada)
    final_merge = final_do_step.merge(
        df_si_subset[df_si_subset["do_detail_id"].notna()],
        how="left",
        on=["do_detail_id", "product_id"],
        suffixes=("", "_si")
    )

    # 8. Saring hanya SO yang valid
    final_merge = final_merge[
        final_merge["so_detail_id"].notna() &
        final_merge["transaction_number_so"].notna()
    ]

    # Pastikan kolom detail_id sudah ada di hasil merge
    # Misalnya: so_detail_id, pr_detail_id, po_detail_id, grn_detail_id, do_detail_id, si_detail_id

    def get_item_status(row):
        if pd.notna(row.get('si_detail_id')):
            return '✅ Sudah sampai Sales Invoice'
        elif pd.notna(row.get('do_detail_id')):
            return '🚚 Sudah sampai Delivery Order'
        elif pd.notna(row.get('grn_detail_id')):
            return '📦 Sudah sampai Goods Receipt'
        elif pd.notna(row.get('po_detail_id')):
            return '📝 Sudah sampai Purchase Order'
        elif pd.notna(row.get('pr_detail_id')):
            return '📄 Masih di Purchase Request'
        else:
            return '⏳ Belum diproses'

    # Tambahkan kolom status_progres ke DataFrame final
    final_merge['status_progres'] = final_merge.apply(get_item_status, axis=1)
    final_merge = apply_search_filter(final_merge, search_number, search_status, search_pic)


    # SO belum DO menggunakan dataset SO Balance final agar konsisten.
    if not df_so_f.empty and "Balance Type" in df_so_f.columns:
        df_so_belum_do = df_so_f[df_so_f["Balance Type"] == "NO DO"].copy()
        total_item_belum_do = int(df_so_belum_do["SO Detail ID"].nunique()) if "SO Detail ID" in df_so_belum_do.columns else len(df_so_belum_do)
        total_dokumen_belum_do = int(df_so_belum_do["No. SO"].nunique()) if "No. SO" in df_so_belum_do.columns else 0
        total_nominal_so_belum_do = float(_safe_numeric_series(df_so_belum_do, "Nominal", 0.0).sum())
    else:
        df_so_belum_do = pd.DataFrame()
        total_item_belum_do = 0
        total_dokumen_belum_do = 0
        total_nominal_so_belum_do = 0.0

    df_customer_pareto = build_customer_pareto(
    df_si_total,
    customer_col="Customer",
    revenue_col="total_si_row",
    transaction_col="transaction_number_si",
    threshold=0.80,
)

    df_pareto_80 = df_customer_pareto[
        df_customer_pareto["Kategori"] == "Pareto 80%"
    ].copy()

    
    # =====================================================
    # LEFT - SO
    # =====================================================
    if selected_doc_type == "SO":
        with col_kiri:
            with st.container(border=True):
                st.subheader("📊 Detail SO")

                c1, c2 = st.columns(2)
                with c1:
                    metric_card("Total SO", f"Rp {total_so:,.0f}".replace(",", "."))
                with c2:
                    metric_card("SO Balance", f"Rp {total_so_unpr:,.0f}".replace(",", "."))

                c1, c2 = st.columns(2)
                with c1:
                    metric_card("Total Transaksi SO", f"{total_so_count:,}")
                with c2:
                    metric_card("Total Transaksi SO Balance", f"{total_so_balance_count:,}".replace(",", "."))

                #c1, c2 = st.columns(2)
                #with c1:
                    #metric_card("Total Nominal SO belum di DOkan",f"Rp {total_nominal_so_belum_do:,.0f}".replace(",", "."))
                #with c2:
                    #metric_card("Total Dokumen belum DO", f"{total_dokumen_belum_do:,}")

                c1, c2 = st.columns(2)
                with c1:
                    metric_card("Revenue", f"Rp {total_si:,.0f}".replace(",", "."))
                with c2:
                    metric_card("Total Item SO", f"{total_so_rows:,}")


                #st.write("Kolom:", df_pr_final_f.columns)
                #st.write("Contoh tanggal:", df_pr_final_f["transaction_date"].head())
                #st.write(df_pr_final_f[["item_price", "item_discount", "item_quantity"]].head())

                c1, c2 = st.columns(2)
                with c1:
                    metric_card("Total Item SO Balance", total_so_balance_rows)
                with c2:
                    metric_card("PIC Terbanyak", top_pic_so)


                so_summary = summarize_status(df_so_f, doc_col="No. SO", nominal_col="Nominal")

                with st.container(border=True):
                    st.subheader("🍩 Proporsi Nominal SO Balance per Status")
                    render_status_pie(so_summary, "Persentase Distribusi Nominal SO Balance")

            pic_summary_so = summarize_pic_status(df_so_f, "PIC Sales", "No. SO")
            with st.container(border=True):
                st.subheader("👤 Analisis Transaksi SO Balance per PIC Sales & per Status")
                render_pic_bar(
                    summary_df=pic_summary_so,
                    x_col="PIC Sales",
                    y_col="Jumlah_Doc",
                    color_col="Status",
                )

            with st.container(border=True):
                st.subheader("🔥 Heatmap SO Balance - Aktivitas PIC Sales")
                render_pic_heatmap(df_so_f, "PIC Sales", "transaction_date", "No. SO", "Heatmap Aktivitas PIC Sales per Bulan")

            # Download PR Balance by status
            with st.container(border=True):
                st.subheader("📥 Download Data SO Balance (NO DO + PARTIAL DO)")

                if not df_so_f.empty and "Status" in df_so_f.columns:
                    all_statuses = sorted([s for s in df_so_f["Status"].dropna().astype(str).unique().tolist() if s.strip()])
                    selected_statuses = st.multiselect(
                        "Pilih Status untuk di-download:",
                        all_statuses,
                        default=all_statuses,
                        key="so_balance_status_export"
                    )

                    df_download_so_balance = df_so_f[df_so_f["Status"].isin(selected_statuses)].copy()

                    if not df_download_so_balance.empty:
                        st.download_button(
                            label=f"⬇️Download {len(df_download_so_balance):,} Baris Data (Filtered).xlsx",
                            data=to_excel_bytes(df_download_so_balance, sheet_name="Data_SO"),
                            file_name=f"Data_SO_Export_{datetime.now().strftime('%Y%m%d')}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                        st.caption(f"Menampilkan {len(df_download_so_balance):,} baris NO DO/PARTIAL DO. Balance Qty = SO Qty - Effective DO Qty.")
                    else:
                        st.warning("Tidak ada data yang sesuai dengan filter yang dipilih.")
                else:
                    st.info("Data SO Balance tidak tersedia untuk export.")

            # Download per PIC PR Balance
            with st.container(border=True):
                st.subheader("📥 Download Data SO Balance per PIC")

                if not df_so_f.empty and "PIC Sales" in df_so_f.columns:
                    # Filter status hanya Need Approve, Approved, In Progress
                    df_filtered_status = df_so_f.copy()
                    #[
                        #df_pr_valid["Status"].isin(["Need Approve", "Approved", "In Progress"])
                    #].copy()

                    # Tambahkan opsi "Semua"
                    options = ["Semua"] + sorted(
                        df_filtered_status["PIC Sales"].fillna("Unassigned").astype(str).unique().tolist()
                    )

                    selected_pic = st.selectbox("Pilih PIC Sales:", options, key="so_balance_pic_select")

                    # Jika pilih "Semua", ambil semua data sesuai status
                    if selected_pic == "Semua":
                        filtered = df_filtered_status.copy()
                    else:
                        filtered = df_filtered_status[
                            df_filtered_status["PIC Sales"].fillna("Unassigned").astype(str) == selected_pic
                        ].copy()

                    st.download_button(
                        label=f"⬇️Download Data {selected_pic}.xlsx",
                        data=to_excel_bytes(filtered, sheet_name="Data_SO_Balance"),
                        file_name=f"Data_SO_balance_{selected_pic}_{datetime.now().strftime('%Y%m%d')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                    st.caption(f"Menampilkan {len(filtered):,} baris data yang akan di-download.")
                else:
                    st.info("Data tidak tersedia untuk fitur download SO Balance per PIC.")

    # =====================================================
    # MID PR
    # =====================================================
        with col_tengah:
            with st.container(border=True):
                    st.subheader("Pareto Customer – 80% Nilai Revenue")

                    if df_customer_pareto.empty:
                        st.info(
                            "Data Pareto belum tersedia. "
                            "Pastikan kolom Customer dan nilai SI tersedia."
                        )
                    else:
                        total_customer = len(df_customer_pareto)
                        total_customer_pareto = len(df_pareto_80)
                        revenue_pareto = df_pareto_80["Revenue"].sum()
                        total_revenue_customer = df_customer_pareto["Revenue"].sum()
                        kontribusi_pareto = (
                            revenue_pareto / total_revenue_customer
                            if total_revenue_customer > 0 else 0
                        )

                        c1, c2, c3 = st.columns(3)

                        with c1:
                            metric_card(
                                "Customer Pareto",
                                f"{total_customer_pareto:,} dari {total_customer:,}"
                            )

                        with c2:
                            metric_card(
                                "Revenue Customer Pareto",
                                f"Rp {revenue_pareto:,.0f}".replace(",", ".")
                            )

                        with c3:
                            metric_card(
                                "Kontribusi Revenue",
                                f"{kontribusi_pareto:.1%}"
                            )

                        display_pareto = df_pareto_80.copy()

                        display_pareto["Revenue"] = display_pareto["Revenue"].map(
                            lambda value: f"Rp {value:,.0f}"
                        )
                        display_pareto["Kontribusi"] = display_pareto["Kontribusi"].map(
                            lambda value: f"{value:.2%}"
                        )
                        display_pareto["Kumulatif"] = display_pareto["Kumulatif"].map(
                            lambda value: f"{value:.2%}"
                        )

                        st.dataframe(
                            display_pareto,
                            use_container_width=True,
                            hide_index=True,
                        )

                        fig_pareto = px.bar(
                            df_pareto_80,
                            x="Customer",
                            y="Revenue",
                            color="Kategori",
                            text="Revenue",
                            title="Customer Penyumbang 80% Nilai SI",
                        )

                        fig_pareto.update_traces(
                            texttemplate="Rp %{text:,.0f}",
                            textposition="outside",
                        )

                        fig_pareto.update_layout(
                            showlegend=False,
                            xaxis_title="Customer",
                            yaxis_title="Nilai SO",
                            yaxis_tickformat=",.0f",
                        )

                        st.plotly_chart(fig_pareto, use_container_width=True)


            with st.container(border=True):
                st.subheader("Customer Concentration")

                if df_customer_concentration.empty:
                    st.info("Data customer concentration belum tersedia.")
                else:
                    c1, c2, c3, c4 = st.columns(4)

                    with c1:
                        metric_card(
                            "Top 1 Customer",
                            f"{concentration_metrics['CR1']:.1%}",
                        )

                    with c2:
                        metric_card(
                            "Top 5 Customer",
                            f"{concentration_metrics['CR5']:.1%}",
                        )

                    with c3:
                        metric_card(
                            "Top 10 Customer",
                            f"{concentration_metrics['CR10']:.1%}",
                        )

                with c4:
                        metric_card(
                            "HHI",
                            f"{concentration_metrics['HHI']:,.0f}",
                        )

                top_customer = df_customer_concentration.head(15).copy()

                fig_concentration = go.Figure()

                fig_concentration.add_trace(
                    go.Bar(
                        x=top_customer["Customer"],
                        y=top_customer["Revenue"],
                        name="Revenue",
                            marker_color="#4C78A8",
                            text=top_customer["Revenue"],
                            texttemplate="Rp %{text:,.0f}",
                            textposition="outside",
                        )
                    )

                fig_concentration.add_trace(
                    go.Scatter(
                        x=top_customer["Customer"],
                        y=top_customer["Cumulative_Share"],
                        name="Kumulatif",
                        mode="lines+markers",
                        line=dict(color="#E45756", width=3),
                        yaxis="y2",
                        hovertemplate="%{y:.1%}<extra></extra>",
                    )
                )

                fig_concentration.update_layout(
                    xaxis=dict(
                        title="Customer",
                        tickangle=-45,
                        automargin=True,
                    ),
                    yaxis=dict(
                        title="Revenue",
                        tickformat=",.0f",
                    ),
                    yaxis2=dict(
                        title="Kontribusi Kumulatif",
                        tickformat=".0%",
                        range=[0, 1.05],
                        overlaying="y",
                        side="right",
                    ),

                    # Pindahkan legend ke atas
                    legend=dict(
                        orientation="h",
                        yanchor="bottom",
                        y=1.08,
                        xanchor="center",
                        x=0.5,
                    ),

                    # Beri ruang untuk legend dan nama customer
                    margin=dict(
                        t=100,
                        b=180,
                        l=80,
                        r=80,
                    ),
                )

                st.plotly_chart(
                    fig_concentration,
                    use_container_width=True,
                )


            with st.container(border=True):
                st.subheader("Customer Retention Bulanan")

                if df_monthly_retention.empty:
                    st.info("Data retention belum tersedia.")
                else:
                    latest_valid = df_monthly_retention[
                        df_monthly_retention["Retention_Rate"].notna()
                
                    ]
                    latest_retention = (
                        latest_valid.iloc[-1]["Retention_Rate"]
                        if not latest_valid.empty
                        else 0
                    )

                    latest_row = df_monthly_retention.iloc[-1]

                    c1, c2, c3, c4 = st.columns(4)

                    with c1:
                        metric_card(
                            "Retention Terakhir",
                            f"{latest_retention:.1%}",
                        )

                    with c2:
                        metric_card(
                            "Customer Aktif",
                            f"{latest_row['Active_Customers']:,.0f}",
                        )

                    with c3:
                        metric_card(
                            "Customer Baru",
                            f"{latest_row['New_Customers']:,.0f}",
                        )

                    with c4:
                        metric_card(
                            "Customer Kembali",
                            f"{latest_row['Returning_Customers']:,.0f}",
                        )

                    fig_retention = go.Figure()

                    fig_retention.add_trace(
                        go.Bar(
                            x=df_monthly_retention["Bulan_Label"],
                            y=df_monthly_retention["New_Customers"],
                            name="Customer Baru",
                            marker_color="#72B7B2",
                        )
                    )

                fig_retention.add_trace(
                    go.Bar(
                        x=df_monthly_retention["Bulan_Label"],
                        y=df_monthly_retention["Returning_Customers"],
                        name="Customer Existing",
                        marker_color="#4C78A8",
                    )
                )

                fig_retention.add_trace(
                    go.Scatter(
                        x=df_monthly_retention["Bulan_Label"],
                        y=df_monthly_retention["Retention_Rate"],
                        name="Retention Rate",
                        mode="lines+markers",
                        line=dict(color="#E45756", width=3),
                        yaxis="y2",
                        hovertemplate="%{y:.1%}<extra></extra>",
                    )
                )

                fig_retention.update_layout(
                        barmode="stack",
                        xaxis_title="Bulan",
                        yaxis_title="Jumlah Customer",
                        yaxis2=dict(
                            title="Retention Rate",
                            tickformat=".0%",
                        range=[0, 1.05],
                            overlaying="y",
                            side="right",
                        ),
                        legend=dict(orientation="h"),
                    )

                st.plotly_chart(
                        fig_retention,
                        use_container_width=True,
                    )

            # Download SO belum DO
            with st.container(border=True):
                st.subheader("📥 Download Data SO belum DO")

                #if not df_so_belum_do.empty and "Status" in df_so_belum_do.columns:
                    #all_statuses = sorted([s for s in df_so_f["Status"].dropna().astype(str).unique().tolist() if s.strip()])
                    #selected_statuses = st.multiselect(
                        #"Pilih Status untuk di-download:",
                        #all_statuses,
                        #default=all_statuses,
                        #key="so_belumDO_status_export"
                    #)

                    #df_download_so_belumDO = df_so_belum_do[df_so_belum_do["Status"].isin(selected_statuses)].copy()
                df_download_so_belumDO = df_so_belum_do.copy()

                if not df_download_so_belumDO.empty:
                    st.download_button(
                        label=f"⬇️Download {len(df_download_so_belumDO):,} Baris Data (Filtered).xlsx",
                        data=to_excel_bytes(df_download_so_belumDO, sheet_name="Data_SO"),
                        file_name=f"Data_SO_Export_{datetime.now().strftime('%Y%m%d')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                    st.caption(f"Menampilkan {len(df_download_so_belumDO):,} baris data yang akan di-download.")
                else:
                    st.warning("Tidak ada data yang sesuai dengan filter yang dipilih.")
            #else:
                #st.info("Data SO belum DO tidak tersedia untuk export.")



            # Download SO belum DO
            with st.container(border=True):
                st.subheader("📥 Download Data Pareto 80%")

                #if not df_so_belum_do.empty and "Status" in df_so_belum_do.columns:
                    #all_statuses = sorted([s for s in df_so_f["Status"].dropna().astype(str).unique().tolist() if s.strip()])
                    #selected_statuses = st.multiselect(
                        #"Pilih Status untuk di-download:",
                        #all_statuses,
                        #default=all_statuses,
                        #key="so_belumDO_status_export"
                    #)

                    #df_download_so_belumDO = df_so_belum_do[df_so_belum_do["Status"].isin(selected_statuses)].copy()
                df_download_pareto_80 = df_pareto_80.copy()

                if not df_download_pareto_80.empty:
                    st.download_button(
                        label=f"⬇️Download {len(df_download_pareto_80):,} Baris Data (Filtered).xlsx",
                        data=to_excel_bytes(df_download_pareto_80, sheet_name="Data_SO"),
                        file_name=f"Data_pareto_Export_{datetime.now().strftime('%Y%m%d')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                    st.caption(f"Menampilkan {len(df_download_pareto_80):,} baris data yang akan di-download.")
                else:
                    st.warning("Tidak ada data yang sesuai dengan filter yang dipilih.")
            #else:
                #st.info("Data SO belum DO tidak tersedia untuk export.")


    from dateutil.relativedelta import relativedelta

    history_start_date = end_date - relativedelta(months=12)

    data_new = load_all_data_new(
        start_date=history_start_date,
        end_date=end_date,
    )       
    # =====================================================
    # RIGHT - SO
    # =====================================================



    # ---------- FOOTER INFO ----------
    with st.expander("ℹ️ Informasi Teknis Dashboard"):
        selected_report_date = (
            selected_date_range[1]
            if isinstance(selected_date_range, (tuple, list)) and len(selected_date_range) == 2
            else date.today()
        )

        st.markdown(
            f"""
- **Base URL:** `{BASE_URL}`
- **Timeout Request:** `{REQUEST_TIMEOUT}` detik
- **Tanggal report sampai:** `{selected_report_date}`
- **Mode filter tanggal:** Total SO mengikuti periode yang dipilih pada Select Date Range
- **Cache API:** 600 detik
            """
        )


if __name__ == "__main__":
    main()
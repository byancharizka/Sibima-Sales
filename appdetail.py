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


BASE_URL = {
    "outstanding": "https://erp.sibima.id/api/dashboard/",
    "erp": "https://erp.sibima.id/api/",
    "brp": "https://brp.sibima.id/api/"
}

API_TOKEN = os.getenv("SIBIMA_API_TOKEN", "d06cd6acd4bff7a3e3b043d3a1b01190e39405b54d3187b1d00a8830dc6d")

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
    endpoint_map = {
        "so": ("so-balance", {"Tanggal": "transaction_date"}),
        "pr": ("pr-balance", {"Tgl. PR": "transaction_date"}),
        "po": ("po-balance", {"Tgl. PO": "transaction_date"}),
        "grn": ("grn-balance", {"Tgl. GRN": "transaction_date"}),
        "do": ("do-balance", {"Tgl. DO": "transaction_date"}),
        "npr": ("outstanding-npr", {"Tanggal": "transaction_date"}),
        #"pur": ("outstanding-pur", {"Tanggal": "transaction_date"})
    }

    result = {}
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



        # Tetapkan tanggal awal khusus untuk SO
        so_start_date = date(2026, 1, 11)   # mulai 11 Januari 2026
        report_end_date = today   # atau sesuai input user

        # Filter SO mulai 11 Januari 2026 sesuai periode user
        df_so_final_real = apply_realization_filter(df_so_final, so_start_date, report_end_date)

        # Dataset lain (PR, PO, GRN, DO, SI) ambil SEMUA data tanpa batasan start_date
        df_pr_final_real = apply_cumulative_filter(df_pr_final, report_end_date)
        df_po_final_real = apply_cumulative_filter(df_po_final, report_end_date)
        df_grn_final_real = apply_cumulative_filter(df_grn_final, report_end_date)
        df_do_final_real = apply_cumulative_filter(df_do_final, report_end_date)
        df_si_final_real = apply_cumulative_filter(df_si_final, report_end_date)

    # ---------- SEARCH FILTER ----------
    df_so_final_f = apply_search_filter(df_so_final_f, search_number, search_status, search_pic)
    df_pr_final_f = apply_search_filter(df_pr_final_f, search_number, search_status, search_pic)
    df_so_final_real = apply_search_filter(df_so_final_real, search_number, search_status, search_pic)
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
    .fillna("")
    .astype(str)
    .str.strip()
    )
    #df_so_total = df_so_final_real[
    #~df_so_final_real["Status"].isin(["Draft"])
    #].copy()
    status_filter = ['In Progress', 'Approved', 'Complete']
    df_so_total = df_so_final_real[df_so_final_real['Status_so'].isin(status_filter)]
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



    df_si_final_real["Status_si"] = (
    df_si_final_real["Status_si"]
    .fillna("")
    .astype(str)
    .str.strip()
    )
    #df_so_total = df_so_final_real[
    #~df_so_final_real["Status"].isin(["Draft"])
    #].copy()
    status_filter2 = ['In Progress', 'Approved', 'Complete', 'Draft' ]
    df_si_total = df_si_final_real[df_si_final_real['Status_si'].isin(status_filter2)]
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
    
    total_so_count = safe_unique_count(df_so_final_real, "transaction_number_so")
    total_so_balance_count = safe_unique_count(df_so_f, "No. SO")
    total_so_rows = len(df_so_final_real)
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


    # Item SO yang sudah mempunyai DO
    so_sudah_do = set(
        final_merge.loc[
            final_merge["do_detail_id"].notna(),
            "so_detail_id"
        ].dropna()
    )

    # Item SO yang sama sekali belum mempunyai DO
    df_so_belum_do = (
        final_merge[
            ~final_merge["so_detail_id"].isin(so_sudah_do)
        ]
        .drop_duplicates(subset=["so_detail_id"])
        .copy()
    )

    total_item_belum_do = df_so_belum_do["so_detail_id"].nunique()
    total_dokumen_belum_do = df_so_belum_do["transaction_number_so"].nunique()
    total_nominal_so_belum_do = df_so_belum_do["nominal_so"].sum()

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

                c1, c2 = st.columns(2)
                with c1:
                    metric_card("Total Nominal SO belum di DOkan",f"Rp {total_nominal_so_belum_do:,.0f}".replace(",", "."))
                with c2:
                    metric_card("Total Dokumen belum DO", f"{total_dokumen_belum_do:,}")

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
                st.subheader("📥 Download Data SO Balance (Periode & Status)")

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
                        st.caption(f"Menampilkan {len(df_download_so_balance):,} baris data yang akan di-download.")
                    else:
                        st.warning("Tidak ada data yang sesuai dengan filter yang dipilih.")
                else:
                    st.info("Data PR Balance tidak tersedia untuk export.")

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


            # Download SO belum DO
            with st.container(border=True):
                st.subheader("📥 Download Data SO belum DO (Periode & Status)")

                if not df_so_belum_do.empty and "Status" in df_so_belum_do.columns:
                    all_statuses = sorted([s for s in df_so_f["Status"].dropna().astype(str).unique().tolist() if s.strip()])
                    selected_statuses = st.multiselect(
                        "Pilih Status untuk di-download:",
                        all_statuses,
                        default=all_statuses,
                        key="so_belumDO_status_export"
                    )

                    df_download_so_belumDO = df_so_belum_do[df_so_belum_do["Status"].isin(selected_statuses)].copy()

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
                else:
                    st.info("Data SO belum DO tidak tersedia untuk export.")



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
- **Mode filter tanggal:** kumulatif (semua data sampai tanggal akhir)
- **Cache API:** 600 detik
            """
        )


if __name__ == "__main__":
    main()
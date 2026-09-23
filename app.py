import logging
import re
from io import BytesIO
from datetime import datetime, date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pytz
import streamlit as st
from sqlalchemy import URL, create_engine, text
from sshtunnel import SSHTunnelForwarder

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
# 3) APP CONFIG + DATABASE CONNECTION
# =========================================================
TIMEZONE = pytz.timezone("Asia/Jakarta")
today = date.today()

DEFAULT_START_DATE = date(today.year, today.month, 1)
DEFAULT_END_DATE = today
DB_CACHE_TTL = 600
CUSTOMER_LOOKUP_VERSION = "2026-09-23-v6-phase3"

# Batas historis minimum untuk SO / SO Balance.
# Hasil rekonsiliasi API menunjukkan SO Balance masih memuat transaksi
# 1-10 Januari 2026, sehingga reader harus tersedia sejak 1 Januari 2026.
# Card Total SO tetap difilter ulang sesuai Select Date Range di main().
SO_BASE_START_DATE = date(2026, 1, 1)


@st.cache_resource
def get_erp_database_connection():
    """
    Koneksi ERP PostgreSQL melalui SSH tunnel.

    Secrets yang dipakai sama dengan script database SIBIMA sebelumnya:

    [ssh]
    host = "..."
    port = 22
    username = "..."
    password = "..."

    [postgres]
    host = "..."
    port = 5432
    username = "..."
    password = "..."
    database = "..."
    """
    ssh = st.secrets["ssh"]
    postgres = st.secrets["postgres"]

    tunnel_kwargs = {
        "ssh_username": ssh["username"],
        "remote_bind_address": (
            postgres["host"],
            int(postgres.get("port", 5432)),
        ),
        "local_bind_address": ("127.0.0.1", 0),
    }
    if ssh.get("password"):
        tunnel_kwargs["ssh_password"] = ssh["password"]
    if ssh.get("private_key"):
        tunnel_kwargs["ssh_pkey"] = ssh["private_key"]

    tunnel = SSHTunnelForwarder(
        (ssh["host"], int(ssh.get("port", 22))),
        **tunnel_kwargs,
    )
    tunnel.start()

    database_url = URL.create(
        drivername="postgresql+psycopg",
        username=postgres["username"],
        password=postgres["password"],
        host="127.0.0.1",
        port=tunnel.local_bind_port,
        database=postgres["database"],
    )

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "connect_timeout": 15,
            "application_name": "sibima_sales_dashboard_database",
        },
    )
    return engine, tunnel


# Mapping tabel ERP. Nama tabel dan relasi mengikuti mapping DB yang sudah
# tervalidasi pada script Weekly Monitoring.
DB_STAGES = {
    "so": {
        "header_table": "public.x4_sales_order",
        "detail_table": "public.x4_sales_order_detail",
        "header_date": "date",
        "header_fk_candidates": ["sales_order_id", "so_id"],
        "detail_id_candidates": ["id", "sales_order_detail_id"],
        "product_candidates": ["item_id", "product_id", "product_detail_id"],
        "item_name_candidates": ["item_name", "product_name", "name"],
    },
    "pr": {
        "header_table": "public.x4_purchase_requests",
        "detail_table": "public.x4_purchase_request_details",
        "header_date": "date",
        "header_fk_candidates": ["purchase_request_id", "pr_id"],
        "detail_id_candidates": ["id", "pr_detail_id", "purchase_request_detail_id"],
        "product_candidates": ["item_id", "product_id", "product_detail_id"],
        "ref_so_candidates": ["so_detail_id", "sales_order_detail_id"],
    },
    "po": {
        "header_table": "public.x4_purchase_orders",
        "detail_table": "public.x4_purchase_order_details",
        "header_date": "date",
        "header_fk_candidates": ["purchase_order_id", "po_id"],
        "detail_id_candidates": ["id", "po_detail_id", "purchase_order_detail_id"],
        "product_candidates": ["item_id", "product_id", "product_detail_id"],
        "ref_pr_candidates": ["pr_detail_id", "purchase_request_detail_id"],
    },
    "grn": {
        "header_table": "public.x4_goods_receipt_note",
        "detail_table": "public.x4_goods_receipt_note_detail",
        "header_date": "transaction_date",
        "header_fk_candidates": ["goods_receipt_note_id", "grn_id", "goods_receipt_id"],
        "detail_id_candidates": ["id", "grn_detail_id", "goods_receipt_note_detail_id"],
        "product_candidates": ["item_id", "product_id", "product_detail_id"],
        "ref_po_candidates": ["purchase_order_detail_id", "po_detail_id"],
    },
    "do": {
        "header_table": "public.x4_delivery_orders",
        "detail_table": "public.x4_delivery_order_details",
        "header_date": "transaction_date",
        "header_fk_candidates": ["delivery_order_id", "do_id"],
        "detail_id_candidates": ["id", "do_detail_id", "delivery_order_detail_id"],
        "product_candidates": ["item_id", "product_id", "product_detail_id"],
        "ref_so_candidates": ["so_detail_id", "sales_order_detail_id"],
        # Tetap dibaca bila kolom memang tersedia, tetapi main dashboard juga
        # mempunyai jalur SO -> DO direct sebagai fallback.
        "ref_grn_candidates": [
            "grn_detail_id",
            "goods_receipt_note_detail_id",
            "goods_receipt_detail_id",
            "receipt_detail_id",
        ],
    },
    "si": {
        "header_table": "public.x4_sales_invoices",
        "detail_table": "public.x4_sales_invoice_details",
        "header_date": "transaction_date",
        "header_fk_candidates": ["sales_invoice_id", "invoice_id", "si_id"],
        "detail_id_candidates": ["id", "si_detail_id", "sales_invoice_detail_id"],
        "product_candidates": ["item_id", "product_id", "product_detail_id"],
        "ref_do_candidates": ["do_detail_id", "delivery_order_detail_id"],
        "item_name_candidates": ["item_name", "product_name", "name"],
    },
}

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



def _calc_line_nominal_for_debug(df: pd.DataFrame) -> pd.Series:
    """Formula nominal generic untuk debug Revenue/SO."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    qty = pd.to_numeric(df.get("item_quantity", 0), errors="coerce").fillna(0)
    price = pd.to_numeric(df.get("item_price", 0), errors="coerce").fillna(0)
    disc = pd.to_numeric(df.get("item_discount", 0), errors="coerce").fillna(0)
    tax = pd.to_numeric(df.get("item_tax1_percentage", 0), errors="coerce").fillna(0)
    disc_unit = price * (disc / 100.0)
    tax_unit = (price - disc_unit) * (tax / 100.0)
    return (qty * (price - disc_unit + tax_unit)).fillna(0)


def build_revenue_debug_package(source_label: str, snapshots: dict[str, pd.DataFrame], app_total_revenue: float):
    """Ringkas pipeline Revenue dan buat workbook audit."""
    summary_rows = []
    prepared_tabs = {}
    for stage, frame in snapshots.items():
        df = frame.copy() if frame is not None else pd.DataFrame()
        if not df.empty:
            df["Debug Revenue Row"] = _calc_line_nominal_for_debug(df)
        else:
            df["Debug Revenue Row"] = pd.Series(dtype="float64")
        prepared_tabs[stage] = df
        date_series = pd.to_datetime(df.get("transaction_date"), errors="coerce") if "transaction_date" in df.columns else pd.Series(dtype="datetime64[ns]")
        summary_rows.append({
            "Stage": stage,
            "Rows": len(df),
            "Unique SI": int(df["transaction_number_si"].nunique()) if "transaction_number_si" in df.columns else 0,
            "Unique SI Detail": int(df["si_detail_id"].nunique()) if "si_detail_id" in df.columns else 0,
            "Unique Customer": int(df["Customer"].nunique()) if "Customer" in df.columns else 0,
            "Total Qty": float(pd.to_numeric(df.get("item_quantity", 0), errors="coerce").fillna(0).sum()) if not df.empty else 0.0,
            "Calculated Revenue": float(pd.to_numeric(df["Debug Revenue Row"], errors="coerce").fillna(0).sum()) if not df.empty else 0.0,
            "Min Date": date_series.min() if not date_series.empty else pd.NaT,
            "Max Date": date_series.max() if not date_series.empty else pd.NaT,
        })
    summary = pd.DataFrame(summary_rows)
    final_calc = 0.0
    if "FINAL_REVENUE" in prepared_tabs and not prepared_tabs["FINAL_REVENUE"].empty:
        final_calc = float(prepared_tabs["FINAL_REVENUE"]["Debug Revenue Row"].sum())
    gap = final_calc - float(app_total_revenue or 0)
    summary["Card Revenue"] = float(app_total_revenue or 0)
    summary["Final Debug vs Card Gap"] = gap

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        summary.to_excel(writer, index=False, sheet_name="SUMMARY")
        for stage, df in prepared_tabs.items():
            sheet = {
                "RAW_SOURCE":"RAW_SI",
                "PERIOD_FILTERED":"PERIOD_SI",
                "STATUS_VALID":"STATUS_SI",
                "FINAL_REVENUE":"FINAL_REVENUE",
            }.get(stage, stage[:31])
            df.to_excel(writer, index=False, sheet_name=sheet[:31])

        final_df = prepared_tabs.get("FINAL_REVENUE", pd.DataFrame()).copy()
        if not final_df.empty:
            if "tax_percentage_source" in final_df.columns:
                tax_source = (
                    final_df.groupby("tax_percentage_source", dropna=False)
                    .agg(
                        Rows=("tax_percentage_source", "size"),
                        Unique_SI=("transaction_number_si", "nunique") if "transaction_number_si" in final_df.columns else ("tax_percentage_source", "size"),
                        Revenue=("Debug Revenue Row", "sum"),
                    )
                    .reset_index()
                )
                tax_source.to_excel(writer, index=False, sheet_name="TAX_SOURCE")
            unresolved = final_df[
                pd.to_numeric(final_df.get("item_tax1_percentage", 0), errors="coerce").fillna(0).le(0)
            ].copy()
            unresolved.to_excel(writer, index=False, sheet_name="UNRESOLVED_TAX")
    return summary, output.getvalue(), gap


def build_so_balance_debug_package(source_label: str, raw_df: pd.DataFrame, filtered_df: pd.DataFrame):
    """Audit sederhana dataset SO Balance tanpa mengubah business rule."""
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
            "Nominal Balance": float(pd.to_numeric(df.get("Nominal", 0), errors="coerce").fillna(0).sum()) if not df.empty else 0.0,
            "NO DO Rows": int((df.get("Balance Type", pd.Series(index=df.index, dtype="object")) == "NO DO").sum()) if not df.empty else 0,
            "PARTIAL DO Rows": int((df.get("Balance Type", pd.Series(index=df.index, dtype="object")) == "PARTIAL DO").sum()) if not df.empty else 0,
            "Min Date": pd.to_datetime(df.get("transaction_date"), errors="coerce").min() if "transaction_date" in df.columns else pd.NaT,
            "Max Date": pd.to_datetime(df.get("transaction_date"), errors="coerce").max() if "transaction_date" in df.columns else pd.NaT,
        })
    summary = pd.DataFrame(rows)
    status_summary = pd.DataFrame()
    if filtered_df is not None and not filtered_df.empty and "Status" in filtered_df.columns:
        tmp = filtered_df.copy()
        tmp["Nominal"] = pd.to_numeric(tmp.get("Nominal", 0), errors="coerce").fillna(0)
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


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def probe_pr_reference_columns(max_rows: int = 20000):
    """
    Probe seluruh candidate reference column pada x4_purchase_request_details.
    Tujuan: menemukan jalur SO -> PR alternatif tanpa menebak nama kolom.
    """
    cfg = DB_STAGES["pr"]
    header_table = cfg["header_table"]
    detail_table = cfg["detail_table"]
    header_cols = get_table_columns(header_table)
    detail_cols = get_table_columns(detail_table)

    detail_id = _first_existing(detail_cols, cfg["detail_id_candidates"])
    detail_header_fk = _first_existing(detail_cols, cfg["header_fk_candidates"])
    header_id = _first_existing(header_cols, ["id"])
    tx_number = _first_existing(header_cols, ["transaction_number", "number", "document_number"])
    header_date = _first_existing(header_cols, [cfg.get("header_date", "date"), "transaction_date", "date"])
    status = _first_existing(header_cols, ["status_description", "status", "realization_status"])
    qty = _first_existing(detail_cols, ["quantity", "item_quantity", "qty", "ordered_quantity"])
    product = _first_existing(detail_cols, cfg.get("product_candidates", []))

    tokens = (
        "so", "sales_order", "salesorder", "reference", "ref_", "source",
        "origin", "parent", "request_from", "request_source",
    )
    candidate_cols = []
    for col in detail_cols:
        key = str(col).lower()
        if any(token in key for token in tokens):
            if col not in {detail_id, detail_header_fk}:
                candidate_cols.append(col)

    metadata = pd.DataFrame({
        "Column": candidate_cols,
        "Selected_Current_SO_Ref": [c == _first_existing(detail_cols, cfg.get("ref_so_candidates", [])) for c in candidate_cols],
    })

    if not candidate_cols or not all([detail_id, detail_header_fk, header_id]):
        return metadata, pd.DataFrame()

    select_parts = [
        f'd.{_quote_ident(detail_id)}::text AS "pr_detail_id"',
        f'h.{_quote_ident(header_id)}::text AS "pr_header_id"',
        f'{("h." + _quote_ident(tx_number) + "::text") if tx_number else "NULL::text"} AS "transaction_number_pr"',
        f'{("h." + _quote_ident(header_date) + "::text") if header_date else "NULL::text"} AS "transaction_date_pr"',
        f'{("h." + _quote_ident(status) + "::text") if status else "NULL::text"} AS "status_pr_raw"',
        f'{("d." + _quote_ident(qty) + "::text") if qty else "NULL::text"} AS "pr_qty"',
        f'{("d." + _quote_ident(product) + "::text") if product else "NULL::text"} AS "product_id"',
    ]
    for col in candidate_cols:
        select_parts.append(f'd.{_quote_ident(col)}::text AS {_quote_ident(col)}')

    where = (
        _soft_delete_sql_predicates("h", header_cols, include_active_current=False)
        + _soft_delete_sql_predicates("d", detail_cols, include_active_current=True)
    )
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    q = text(f"""
        SELECT {', '.join(select_parts)}
        FROM {header_table} h
        JOIN {detail_table} d
          ON h.{_quote_ident(header_id)} = d.{_quote_ident(detail_header_fk)}
        {where_sql}
        LIMIT {int(max_rows)}
    """)
    engine, _ = get_erp_database_connection()
    try:
        with engine.connect() as conn:
            sample = pd.read_sql_query(q, conn)
    except Exception as exc:
        logger.warning("PR reference probe gagal: %s", exc)
        sample = pd.DataFrame()
    if not sample.empty and "status_pr_raw" in sample.columns:
        sample["status_pr"] = sample["status_pr_raw"].map(normalize_api_status)
    return metadata, sample



# =========================================================
# PHASE 5 - SO BALANCE MEMBERSHIP / BUSINESS FIELD FORENSIC
# =========================================================
SO_BALANCE_PR_PROGRESS_STATUSES = {"Complete"}
SO_BALANCE_DO_PROGRESS_STATUSES = {"Approved", "In Progress", "Complete"}
SO_BALANCE_LATEST_PR_BLOCK_STATUSES = {"Need Approve", "In Progress"}
SO_BALANCE_SERVICE_PATTERN = r"Jasa|Biaya|Pengiriman|Shipping|Manage Service|Kalibrasi"


def _business_candidate_columns(columns: list[str], max_cols: int = 120) -> list[str]:
    """Pilih field yang berpotensi menjelaskan hidden membership rule SO Balance."""
    tokens = (
        "type", "category", "class", "group", "segment", "source", "origin",
        "stock", "inventory", "warehouse", "purchase", "procurement", "buy",
        "service", "project", "balance", "outstanding", "status", "realization",
        "realisation", "parent", "revision", "version", "current", "latest",
        "active", "deleted", "is_", "flow", "method", "channel", "fulfill",
        "delivery", "direct", "indent", "tender", "contract", "division",
        "department", "business", "order", "description", "remark", "note",
        "purpose", "request", "quotation", "quote", "customer", "vendor",
        "supplier", "brand", "material", "product", "item", "code", "kind",
        "flag", "mode", "scheme", "schema", "route", "path",
    )
    priority = []
    for col in columns:
        key = str(col).lower()
        score = sum(1 for token in tokens if token in key)
        if score:
            priority.append((score, str(col)))
    priority.sort(key=lambda x: (-x[0], x[1]))
    return [col for _, col in priority[:max_cols]]


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def load_so_membership_business_fields(end_date=None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Ambil candidate business fields dari SO header/detail dan product master (jika FK ada).
    Semua candidate value dicast ke text agar aman diekspor untuk forensic comparison.
    """
    cfg = DB_STAGES["so"]
    header_table = cfg["header_table"]
    detail_table = cfg["detail_table"]
    header_cols = get_table_columns(header_table)
    detail_cols = get_table_columns(detail_table)

    header_id = _first_existing(header_cols, ["id"] + cfg.get("header_id_candidates", []))
    detail_fk = _first_existing(detail_cols, cfg["header_fk_candidates"])
    detail_id = _first_existing(detail_cols, cfg["detail_id_candidates"])
    product_col = _first_existing(detail_cols, cfg["product_candidates"])
    tx_number = _first_existing(header_cols, ["transaction_number", "number", "document_number"])
    header_date = _first_existing(header_cols, [cfg["header_date"], "transaction_date", "date"])

    if not all([header_id, detail_fk, detail_id]):
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    h_candidates = _business_candidate_columns(header_cols)
    d_candidates = _business_candidate_columns(detail_cols)

    product_ref = discover_fk_reference(detail_table, product_col) if product_col else None
    p_candidates = []
    product_join = ""
    if product_ref:
        try:
            p_cols = get_table_columns(product_ref["table"])
            p_candidates = _business_candidate_columns(p_cols, max_cols=80)
            product_join = (
                f" LEFT JOIN {product_ref['table']} p"
                f" ON d.{_quote_ident(product_col)} = p.{_quote_ident(product_ref['id_col'])}"
            )
        except Exception:
            p_candidates = []
            product_join = ""

    select_parts = [
        f'd.{_quote_ident(detail_id)}::text AS "SO Detail Key"',
        f'{("h." + _quote_ident(tx_number) + "::text") if tx_number else "NULL::text"} AS "No. SO"',
        f'{("h." + _quote_ident(header_date) + "::text") if header_date else "NULL::text"} AS "SO Date Raw"',
        f'{("d." + _quote_ident(product_col) + "::text") if product_col else "NULL::text"} AS "Product Internal ID"',
    ]
    metadata_rows = []

    for col in h_candidates:
        alias = f"H__{col}"
        select_parts.append(f'h.{_quote_ident(col)}::text AS {_quote_ident(alias)}')
        metadata_rows.append({"Alias": alias, "Source": "SO_HEADER", "Table": header_table, "Column": col})
    for col in d_candidates:
        alias = f"D__{col}"
        select_parts.append(f'd.{_quote_ident(col)}::text AS {_quote_ident(alias)}')
        metadata_rows.append({"Alias": alias, "Source": "SO_DETAIL", "Table": detail_table, "Column": col})
    if product_join:
        for col in p_candidates:
            alias = f"P__{col}"
            select_parts.append(f'p.{_quote_ident(col)}::text AS {_quote_ident(alias)}')
            metadata_rows.append({"Alias": alias, "Source": "PRODUCT_MASTER", "Table": product_ref["table"], "Column": col})

    params = {}
    where = []
    if header_date:
        params["start_date"] = pd.Timestamp(SO_BASE_START_DATE)
        where.append(f'h.{_quote_ident(header_date)} >= :start_date')
        if end_date is not None:
            params["next_date"] = pd.Timestamp(end_date) + pd.Timedelta(days=1)
            where.append(f'h.{_quote_ident(header_date)} < :next_date')
    where.extend(_soft_delete_sql_predicates("h", header_cols, include_active_current=False))
    where.extend(_soft_delete_sql_predicates("d", detail_cols, include_active_current=True))
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    query = text(f"""
        SELECT {', '.join(select_parts)}
        FROM {header_table} h
        JOIN {detail_table} d
          ON h.{_quote_ident(header_id)} = d.{_quote_ident(detail_fk)}
        {product_join}
        {where_sql}
    """)

    engine, _ = get_erp_database_connection()
    try:
        with engine.connect() as conn:
            values = pd.read_sql_query(query, conn, params=params)
    except Exception as exc:
        logger.warning("SO membership business-field probe gagal: %s", exc)
        values = pd.DataFrame()

    metadata = pd.DataFrame(metadata_rows)
    source_meta = pd.DataFrame([{
        "SO Header": header_table,
        "SO Detail": detail_table,
        "Product Source Column": product_col or "-",
        "Product Master": product_ref.get("table", "-") if product_ref else "-",
        "Product Master ID": product_ref.get("id_col", "-") if product_ref else "-",
        "Header Candidate Fields": len(h_candidates),
        "Detail Candidate Fields": len(d_candidates),
        "Product Candidate Fields": len(p_candidates),
    }])
    return metadata, values, source_meta


def _field_profile(df: pd.DataFrame, metadata: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if df is None or df.empty or metadata is None or metadata.empty:
        return pd.DataFrame()
    for rec in metadata.to_dict("records"):
        alias = rec.get("Alias")
        if not alias or alias not in df.columns:
            continue
        s = df[alias].fillna("").astype(str).str.strip()
        nonblank = s[s.ne("")]
        top = nonblank.value_counts(dropna=False).head(8)
        top_text = " | ".join(f"{idx} ({cnt})" for idx, cnt in top.items())
        rows.append({
            **rec,
            "Rows": len(df),
            "NonBlank": int(nonblank.shape[0]),
            "Unique": int(nonblank.nunique()),
            "Top Values": top_text,
        })
    return pd.DataFrame(rows).sort_values(["NonBlank", "Unique"], ascending=[False, True]) if rows else pd.DataFrame()


def build_phase5_balance_membership_package(
    so_df: pd.DataFrame,
    pr_df: pd.DataFrame,
    do_df: pd.DataFrame,
    business_metadata: pd.DataFrame,
    business_values: pd.DataFrame,
    business_source_meta: pd.DataFrame,
) -> tuple[pd.DataFrame, bytes]:
    """
    Reconstruct API quantity rule, tetapi TIDAK mengganti production SO Balance card.
    Tujuan Phase 5: mencari hidden membership rule secara aman.
    """
    so = so_df.copy() if so_df is not None else pd.DataFrame()
    pr = pr_df.copy() if pr_df is not None else pd.DataFrame()
    do = do_df.copy() if do_df is not None else pd.DataFrame()

    if so.empty:
        out = BytesIO()
        with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
            pd.DataFrame([{"Message": "SO source kosong"}]).to_excel(writer, index=False, sheet_name="SUMMARY")
        return pd.DataFrame(), out.getvalue()

    so["SO Detail Key"] = so.get("item_id", pd.Series(index=so.index, dtype="object")).map(_canonical_db_key)
    so["Product Key"] = so.get("item_product_id", pd.Series(index=so.index, dtype="object")).map(_canonical_db_key)
    so["No. SO"] = so.get("transaction_number", pd.Series("", index=so.index)).fillna("").astype(str)
    so["SO Date"] = pd.to_datetime(so.get("transaction_date"), errors="coerce")
    so["SO Status"] = so.get("status_description", pd.Series("", index=so.index)).map(normalize_api_status)
    so["PIC Sales"] = so.get("pic_sales_name", pd.Series("", index=so.index)).fillna("").astype(str).str.strip()
    so["Item Name"] = so.get("item_item_name", pd.Series("", index=so.index)).fillna("").astype(str).str.strip()
    so["SO Qty"] = pd.to_numeric(so.get("item_quantity", 0), errors="coerce").fillna(0).clip(lower=0)
    so["Net Unit Price"] = _so_net_unit_price(so)
    so = so.drop_duplicates(["SO Detail Key", "Product Key"], keep="first")

    # ---------------- PR: API progress only COMPLETE ----------------
    pr_complete_detail = pd.DataFrame()
    pr_summary = pd.DataFrame(columns=["SO Detail Key", "Product Key"])
    if not pr.empty:
        p = pr.copy()
        p["SO Detail Key"] = p.get("item_so_detail_id", pd.Series(index=p.index, dtype="object")).map(_canonical_db_key)
        p["Product Key"] = p.get("item_product_id", pd.Series(index=p.index, dtype="object")).map(_canonical_db_key)
        p["PR Detail Key"] = p.get("item_id", pd.Series(index=p.index, dtype="object")).map(_canonical_db_key)
        p["PR Qty"] = pd.to_numeric(p.get("item_quantity", 0), errors="coerce").fillna(0).clip(lower=0)
        p["PR Status"] = p.get("status_description", pd.Series("", index=p.index)).map(normalize_api_status)
        p["No. PR"] = p.get("transaction_number", pd.Series("", index=p.index)).fillna("").astype(str)
        p["PR Date"] = pd.to_datetime(p.get("transaction_date"), errors="coerce")
        p = p[p["SO Detail Key"].notna()].drop_duplicates("PR Detail Key", keep="first")

        # Latest PR is diagnostic membership evidence; progress uses Complete only.
        latest = (
            p.sort_values(["PR Date", "PR Detail Key"], na_position="first")
             .groupby(["SO Detail Key", "Product Key"], dropna=False)
             .tail(1)
             [["SO Detail Key", "Product Key", "No. PR", "PR Status", "PR Date", "PR Detail Key"]]
             .rename(columns={
                 "No. PR": "Latest PR", "PR Status": "Latest PR Status",
                 "PR Date": "Latest PR Date", "PR Detail Key": "Latest PR Detail ID"
             })
        )

        pr_complete_detail = p[p["PR Status"].isin(SO_BALANCE_PR_PROGRESS_STATUSES)].copy()
        strict = pr_complete_detail[pr_complete_detail["Product Key"].notna()].copy()
        if not strict.empty:
            pr_summary = strict.groupby(["SO Detail Key", "Product Key"], dropna=False).agg(
                **{
                    "PR Complete Qty Strict": ("PR Qty", "sum"),
                    "PR Complete Documents": ("No. PR", _join_unique_text),
                    "PR Complete Detail Count": ("PR Detail Key", "nunique"),
                }
            ).reset_index()
        detail_only = pr_complete_detail.groupby("SO Detail Key", dropna=False)["PR Qty"].sum().reset_index(name="PR Complete Qty Detail")
        all_statuses = p.groupby(["SO Detail Key", "Product Key"], dropna=False).agg(
            **{
                "PR All Statuses": ("PR Status", _join_unique_text),
                "PR All Documents": ("No. PR", _join_unique_text),
                "PR All Detail Count": ("PR Detail Key", "nunique"),
            }
        ).reset_index()
        pr_summary = so[["SO Detail Key", "Product Key"]].merge(pr_summary, how="left", on=["SO Detail Key", "Product Key"])
        pr_summary = pr_summary.merge(detail_only, how="left", on="SO Detail Key")
        pr_summary = pr_summary.merge(all_statuses, how="left", on=["SO Detail Key", "Product Key"])
        pr_summary = pr_summary.merge(latest, how="left", on=["SO Detail Key", "Product Key"])

    # ---------------- DO: API progress Approved/In Progress/Complete ----------------
    do_progress_detail = pd.DataFrame()
    do_summary = pd.DataFrame(columns=["SO Detail Key", "Product Key"])
    if not do.empty:
        d = do.copy()
        d["SO Detail Key"] = d.get("item_so_detail_id", pd.Series(index=d.index, dtype="object")).map(_canonical_db_key)
        d["Product Key"] = d.get("item_product_id", pd.Series(index=d.index, dtype="object")).map(_canonical_db_key)
        d["DO Detail Key"] = d.get("item_id", pd.Series(index=d.index, dtype="object")).map(_canonical_db_key)
        d["DO Qty"] = pd.to_numeric(d.get("item_quantity", 0), errors="coerce").fillna(0).clip(lower=0)
        d["DO Status"] = d.get("status_description", pd.Series("", index=d.index)).map(normalize_api_status)
        d["No. DO"] = d.get("transaction_number", pd.Series("", index=d.index)).fillna("").astype(str)
        d = d[d["SO Detail Key"].notna()].drop_duplicates("DO Detail Key", keep="first")
        do_progress_detail = d[d["DO Status"].isin(SO_BALANCE_DO_PROGRESS_STATUSES)].copy()
        strict = do_progress_detail[do_progress_detail["Product Key"].notna()].copy()
        if not strict.empty:
            do_summary = strict.groupby(["SO Detail Key", "Product Key"], dropna=False).agg(
                **{
                    "DO Progress Qty Strict": ("DO Qty", "sum"),
                    "DO Progress Documents": ("No. DO", _join_unique_text),
                    "DO Progress Detail Count": ("DO Detail Key", "nunique"),
                }
            ).reset_index()
        detail_only = do_progress_detail.groupby("SO Detail Key", dropna=False)["DO Qty"].sum().reset_index(name="DO Progress Qty Detail")
        all_statuses = d.groupby(["SO Detail Key", "Product Key"], dropna=False).agg(
            **{
                "DO All Statuses": ("DO Status", _join_unique_text),
                "DO All Documents": ("No. DO", _join_unique_text),
            }
        ).reset_index()
        do_summary = so[["SO Detail Key", "Product Key"]].merge(do_summary, how="left", on=["SO Detail Key", "Product Key"])
        do_summary = do_summary.merge(detail_only, how="left", on="SO Detail Key")
        do_summary = do_summary.merge(all_statuses, how="left", on=["SO Detail Key", "Product Key"])

    ev = so[["SO Detail Key", "Product Key", "No. SO", "SO Date", "SO Status", "PIC Sales", "Item Name", "SO Qty", "Net Unit Price"]].copy()
    if not pr_summary.empty:
        ev = ev.merge(pr_summary, how="left", on=["SO Detail Key", "Product Key"])
    if not do_summary.empty:
        ev = ev.merge(do_summary, how="left", on=["SO Detail Key", "Product Key"])

    for c in ["PR Complete Qty Strict", "PR Complete Qty Detail", "DO Progress Qty Strict", "DO Progress Qty Detail"]:
        if c not in ev.columns:
            ev[c] = 0.0
        ev[c] = pd.to_numeric(ev[c], errors="coerce").fillna(0.0)
    ev["PR Qty API Rule"] = ev[["PR Complete Qty Strict", "PR Complete Qty Detail"]].max(axis=1)
    ev["DO Qty API Rule"] = ev[["DO Progress Qty Strict", "DO Progress Qty Detail"]].max(axis=1)
    ev["Effective Progress API Rule"] = ev[["PR Qty API Rule", "DO Qty API Rule"]].max(axis=1)
    ev["Balance Qty API Rule"] = (ev["SO Qty"] - ev["Effective Progress API Rule"]).clip(lower=0)
    ev["Nominal API Formula"] = ev["Balance Qty API Rule"] * ev["Net Unit Price"]
    ev["Eligible SO Status"] = ~ev["SO Status"].isin(SO_BALANCE_EXCLUDED_STATUSES)
    ev["API Formula Candidate"] = ev["Eligible SO Status"] & ev["Balance Qty API Rule"].gt(0)

    if "Latest PR Status" not in ev.columns:
        ev["Latest PR Status"] = ""
    ev["Latest PR Status"] = ev["Latest PR Status"].fillna("").astype(str)
    ev["Blocked Latest PR State"] = ev["Latest PR Status"].isin(SO_BALANCE_LATEST_PR_BLOCK_STATUSES)
    ev["Service Keyword Flag"] = ev["Item Name"].str.contains(SO_BALANCE_SERVICE_PATTERN, case=False, na=False, regex=True)
    ev["PIC Aulia Diagnostic"] = ev["PIC Sales"].str.strip().str.lower().eq("aulia rahman")
    ev["Known Rule Candidate"] = (
        ev["API Formula Candidate"]
        & ~ev["Blocked Latest PR State"]
        & ~ev["Service Keyword Flag"]
    )

    # Merge runtime business fields for forensic discovery.
    field_values = business_values.copy() if business_values is not None else pd.DataFrame()
    if not field_values.empty and "SO Detail Key" in field_values.columns:
        field_values["SO Detail Key"] = field_values["SO Detail Key"].map(_canonical_db_key)
        field_values = field_values.drop_duplicates("SO Detail Key", keep="last")
        ev_fields = ev.merge(field_values.drop(columns=["No. SO", "SO Date Raw", "Product Internal ID"], errors="ignore"), how="left", on="SO Detail Key")
    else:
        ev_fields = ev.copy()

    api_formula = ev_fields[ev_fields["API Formula Candidate"]].copy()
    known_rule = ev_fields[ev_fields["Known Rule Candidate"]].copy()
    profile = _field_profile(known_rule, business_metadata)

    summary = pd.DataFrame([
        {"Metric": "API Formula Candidate Rows", "Value": len(api_formula)},
        {"Metric": "API Formula Candidate SO", "Value": int(api_formula["No. SO"].nunique()) if not api_formula.empty else 0},
        {"Metric": "API Formula Candidate Nominal", "Value": float(api_formula["Nominal API Formula"].sum()) if not api_formula.empty else 0.0},
        {"Metric": "Blocked Latest PR Rows", "Value": int(api_formula["Blocked Latest PR State"].sum()) if not api_formula.empty else 0},
        {"Metric": "Service Keyword Rows", "Value": int(api_formula["Service Keyword Flag"].sum()) if not api_formula.empty else 0},
        {"Metric": "PIC Aulia Diagnostic Rows", "Value": int(api_formula["PIC Aulia Diagnostic"].sum()) if not api_formula.empty else 0},
        {"Metric": "Known Rule Candidate Rows", "Value": len(known_rule)},
        {"Metric": "Known Rule Candidate SO", "Value": int(known_rule["No. SO"].nunique()) if not known_rule.empty else 0},
        {"Metric": "Known Rule Candidate Nominal", "Value": float(known_rule["Nominal API Formula"].sum()) if not known_rule.empty else 0.0},
    ])

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        summary.to_excel(writer, index=False, sheet_name="SUMMARY")
        api_formula.to_excel(writer, index=False, sheet_name="API_FORMULA_CANDIDATES")
        known_rule.to_excel(writer, index=False, sheet_name="KNOWN_RULE_CANDIDATES")
        pr_complete_detail.to_excel(writer, index=False, sheet_name="PR_COMPLETE_DETAIL")
        do_progress_detail.to_excel(writer, index=False, sheet_name="DO_PROGRESS_DETAIL")
        if business_source_meta is not None and not business_source_meta.empty:
            business_source_meta.to_excel(writer, index=False, sheet_name="BUSINESS_SOURCE_META")
        if business_metadata is not None and not business_metadata.empty:
            business_metadata.to_excel(writer, index=False, sheet_name="BUSINESS_FIELD_META")
        if not profile.empty:
            profile.to_excel(writer, index=False, sheet_name="FIELD_PROFILE")
        if not business_values.empty:
            business_values.to_excel(writer, index=False, sheet_name="ALL_BUSINESS_FIELDS")
    return summary, output.getvalue()


def build_so_balance_evidence_package(
    so_df: pd.DataFrame,
    pr_df: pd.DataFrame,
    do_df: pd.DataFrame,
    si_df: pd.DataFrame,
    current_balance_df: pd.DataFrame,
    pr_ref_metadata: pd.DataFrame | None = None,
    pr_ref_sample: pd.DataFrame | None = None,
):
    """
    Phase 4 forensic workbook untuk SO Balance.

    Tidak mengubah business rule card. Workbook ini membandingkan beberapa kandidat
    PR progress (SUM, MAX single detail, LATEST PR detail) untuk setiap SO detail dan
    menampilkan evidence PR/DO yang mendasarinya.
    """
    so = so_df.copy() if so_df is not None else pd.DataFrame()
    pr = pr_df.copy() if pr_df is not None else pd.DataFrame()
    do = do_df.copy() if do_df is not None else pd.DataFrame()
    si = si_df.copy() if si_df is not None else pd.DataFrame()
    bal = current_balance_df.copy() if current_balance_df is not None else pd.DataFrame()

    if so.empty:
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            pd.DataFrame([{"Message":"SO source kosong"}]).to_excel(writer, index=False, sheet_name="SUMMARY")
        return pd.DataFrame(), output.getvalue()

    so["SO Detail Key"] = so.get("item_id", pd.Series(index=so.index, dtype="object")).map(_canonical_db_key)
    so["Product Key"] = so.get("item_product_id", pd.Series(index=so.index, dtype="object")).map(_canonical_db_key)
    so["SO Qty"] = pd.to_numeric(so.get("item_quantity", 0), errors="coerce").fillna(0).clip(lower=0)
    so["Net Unit Price"] = _so_net_unit_price(so)
    so["SO Status"] = so.get("status_description", pd.Series("", index=so.index)).map(normalize_api_status)
    so["PIC Sales"] = so.get("pic_sales_name", pd.Series("", index=so.index))
    so["No. SO"] = so.get("transaction_number", pd.Series("", index=so.index))
    so["SO Date"] = pd.to_datetime(so.get("transaction_date"), errors="coerce")
    so["Item Name"] = so.get("item_item_name", pd.Series("", index=so.index))
    so["SO Detail DO Qty"] = pd.to_numeric(so.get("item_do_quantity", 0), errors="coerce").fillna(0).clip(lower=0)
    so["SO Detail SI Qty"] = pd.to_numeric(so.get("item_si_quantity", 0), errors="coerce").fillna(0).clip(lower=0)
    so = so.drop_duplicates(["SO Detail Key", "Product Key"], keep="first")

    # ---------- PR detail evidence ----------
    pr_evidence = pd.DataFrame()
    pr_summary = pd.DataFrame(columns=["SO Detail Key","Product Key"])
    if not pr.empty:
        pr_evidence = pd.DataFrame(index=pr.index)
        pr_evidence["PR Detail ID"] = pr.get("item_id")
        pr_evidence["PR Detail Key"] = pr.get("item_id", pd.Series(index=pr.index, dtype="object")).map(_canonical_db_key)
        pr_evidence["No. PR"] = pr.get("transaction_number")
        pr_evidence["PR Date"] = pd.to_datetime(pr.get("transaction_date"), errors="coerce")
        pr_evidence["PR Status"] = pr.get("status_description", pd.Series("", index=pr.index)).map(normalize_api_status)
        pr_evidence["SO Detail ID Ref"] = pr.get("item_so_detail_id")
        pr_evidence["SO Detail Key"] = pr.get("item_so_detail_id", pd.Series(index=pr.index, dtype="object")).map(_canonical_db_key)
        pr_evidence["Product ID"] = pr.get("item_product_id")
        pr_evidence["Product Key"] = pr.get("item_product_id", pd.Series(index=pr.index, dtype="object")).map(_canonical_db_key)
        pr_evidence["PR Qty"] = pd.to_numeric(pr.get("item_quantity", 0), errors="coerce").fillna(0).clip(lower=0)
        pr_evidence = pr_evidence[pr_evidence["SO Detail Key"].notna()].copy()
        pr_evidence = pr_evidence.drop_duplicates("PR Detail Key", keep="first")

        strict = pr_evidence[pr_evidence["Product Key"].notna()].copy()
        if not strict.empty:
            grouped_rows = []
            for (so_key, prod_key), g in strict.groupby(["SO Detail Key","Product Key"], dropna=False):
                g2 = g.sort_values(["PR Date","PR Detail Key"], na_position="first")
                latest = g2.iloc[-1]
                grouped_rows.append({
                    "SO Detail Key": so_key,
                    "Product Key": prod_key,
                    "PR Qty SUM": float(g["PR Qty"].sum()),
                    "PR Qty MAX": float(g["PR Qty"].max()),
                    "PR Qty LATEST": float(latest["PR Qty"]),
                    "PR Detail Count": int(g["PR Detail Key"].nunique()),
                    "PR Document Count": int(g["No. PR"].nunique()),
                    "PR Documents": _join_unique_text(g["No. PR"]),
                    "PR Statuses": _join_unique_text(g["PR Status"]),
                    "Latest PR": latest["No. PR"],
                    "Latest PR Status": latest["PR Status"],
                    "Latest PR Date": latest["PR Date"],
                    "Latest PR Detail ID": latest["PR Detail ID"],
                })
            pr_summary = pd.DataFrame(grouped_rows)

    # ---------- DO evidence ----------
    do_summary = pd.DataFrame(columns=["SO Detail Key","Product Key"])
    do_evidence = pd.DataFrame()
    if not do.empty:
        do_evidence = pd.DataFrame(index=do.index)
        do_evidence["DO Detail ID"] = do.get("item_id")
        do_evidence["DO Detail Key"] = do.get("item_id", pd.Series(index=do.index, dtype="object")).map(_canonical_db_key)
        do_evidence["No. DO"] = do.get("transaction_number")
        do_evidence["DO Date"] = pd.to_datetime(do.get("transaction_date"), errors="coerce")
        do_evidence["DO Status"] = do.get("status_description", pd.Series("", index=do.index)).map(normalize_api_status)
        do_evidence["SO Detail ID Ref"] = do.get("item_so_detail_id")
        do_evidence["SO Detail Key"] = do.get("item_so_detail_id", pd.Series(index=do.index, dtype="object")).map(_canonical_db_key)
        do_evidence["Product ID"] = do.get("item_product_id")
        do_evidence["Product Key"] = do.get("item_product_id", pd.Series(index=do.index, dtype="object")).map(_canonical_db_key)
        do_evidence["DO Qty"] = pd.to_numeric(do.get("item_quantity", 0), errors="coerce").fillna(0).clip(lower=0)
        do_evidence = do_evidence[do_evidence["SO Detail Key"].notna()].copy()
        do_evidence = do_evidence.drop_duplicates("DO Detail Key", keep="first")
        strict_do = do_evidence[do_evidence["Product Key"].notna()].copy()
        if not strict_do.empty:
            do_summary = (
                strict_do.groupby(["SO Detail Key","Product Key"], dropna=False)
                .agg(
                    **{
                        "DO Qty Direct SUM": ("DO Qty","sum"),
                        "DO Detail Count": ("DO Detail Key","nunique"),
                        "DO Document Count": ("No. DO","nunique"),
                        "DO Documents": ("No. DO", _join_unique_text),
                        "DO Statuses": ("DO Status", _join_unique_text),
                    }
                )
                .reset_index()
            )

    evidence = so[[
        "SO Detail Key","Product Key","No. SO","SO Date","SO Status","PIC Sales",
        "Item Name","SO Qty","Net Unit Price","SO Detail DO Qty","SO Detail SI Qty"
    ]].copy()
    evidence = evidence.merge(pr_summary, how="left", on=["SO Detail Key","Product Key"])
    evidence = evidence.merge(do_summary, how="left", on=["SO Detail Key","Product Key"])

    numeric_fill = [
        "PR Qty SUM","PR Qty MAX","PR Qty LATEST","PR Detail Count","PR Document Count",
        "DO Qty Direct SUM","DO Detail Count","DO Document Count",
    ]
    for col in numeric_fill:
        if col not in evidence.columns:
            evidence[col] = 0
        evidence[col] = pd.to_numeric(evidence[col], errors="coerce").fillna(0)
    for col in ["PR Documents","PR Statuses","Latest PR","Latest PR Status","DO Documents","DO Statuses"]:
        if col not in evidence.columns:
            evidence[col] = ""
        evidence[col] = evidence[col].fillna("").astype(str)

    evidence["Effective DO Qty"] = evidence[["SO Detail DO Qty","DO Qty Direct SUM","SO Detail SI Qty"]].max(axis=1)
    evidence["Progress SUM_PR"] = evidence[["PR Qty SUM","Effective DO Qty"]].max(axis=1)
    evidence["Progress MAX_PR"] = evidence[["PR Qty MAX","Effective DO Qty"]].max(axis=1)
    evidence["Progress LATEST_PR"] = evidence[["PR Qty LATEST","Effective DO Qty"]].max(axis=1)
    evidence["Balance Qty SUM_PR"] = (evidence["SO Qty"] - evidence["Progress SUM_PR"]).clip(lower=0)
    evidence["Balance Qty MAX_PR"] = (evidence["SO Qty"] - evidence["Progress MAX_PR"]).clip(lower=0)
    evidence["Balance Qty LATEST_PR"] = (evidence["SO Qty"] - evidence["Progress LATEST_PR"]).clip(lower=0)
    evidence["Nominal SUM_PR"] = evidence["Balance Qty SUM_PR"] * evidence["Net Unit Price"]
    evidence["Nominal MAX_PR"] = evidence["Balance Qty MAX_PR"] * evidence["Net Unit Price"]
    evidence["Nominal LATEST_PR"] = evidence["Balance Qty LATEST_PR"] * evidence["Net Unit Price"]
    evidence["Has Multiple PR Details"] = evidence["PR Detail Count"] > 1
    evidence["Has Multiple PR Docs"] = evidence["PR Document Count"] > 1

    # Membership current Phase 3 balance.
    if not bal.empty:
        bal_map = bal.copy()
        bal_map["SO Detail Key"] = bal_map.get("SO Detail ID", pd.Series(index=bal_map.index, dtype="object")).map(_canonical_db_key)
        bal_map["Current Balance Qty"] = pd.to_numeric(bal_map.get("Balance Qty", 0), errors="coerce").fillna(0)
        bal_map["Current Balance Nominal"] = pd.to_numeric(bal_map.get("Nominal", 0), errors="coerce").fillna(0)
        bal_small = bal_map[["SO Detail Key","Current Balance Qty","Current Balance Nominal"]].drop_duplicates("SO Detail Key")
        evidence = evidence.merge(bal_small, how="left", on="SO Detail Key")
    else:
        evidence["Current Balance Qty"] = 0.0
        evidence["Current Balance Nominal"] = 0.0
    evidence["Current Balance Qty"] = pd.to_numeric(evidence.get("Current Balance Qty",0), errors="coerce").fillna(0)
    evidence["Current Balance Nominal"] = pd.to_numeric(evidence.get("Current Balance Nominal",0), errors="coerce").fillna(0)
    evidence["Included Current Balance"] = evidence["Current Balance Qty"] > 0

    # Status eligibility according to current dashboard balance rule.
    evidence["Eligible SO Status"] = ~evidence["SO Status"].isin(SO_BALANCE_EXCLUDED_STATUSES)

    summary = pd.DataFrame([
        {
            "Metric":"SO Current Detail Rows", "Value": len(evidence)
        },
        {
            "Metric":"Current Balance Rows", "Value": int(evidence["Included Current Balance"].sum())
        },
        {
            "Metric":"Rows Multiple PR Details", "Value": int(evidence["Has Multiple PR Details"].sum())
        },
        {
            "Metric":"Rows Multiple PR Documents", "Value": int(evidence["Has Multiple PR Docs"].sum())
        },
        {
            "Metric":"Nominal Current Balance", "Value": float(evidence["Current Balance Nominal"].sum())
        },
        {
            "Metric":"Nominal Candidate SUM_PR", "Value": float(evidence.loc[evidence["Eligible SO Status"], "Nominal SUM_PR"].sum())
        },
        {
            "Metric":"Nominal Candidate MAX_PR", "Value": float(evidence.loc[evidence["Eligible SO Status"], "Nominal MAX_PR"].sum())
        },
        {
            "Metric":"Nominal Candidate LATEST_PR", "Value": float(evidence.loc[evidence["Eligible SO Status"], "Nominal LATEST_PR"].sum())
        },
    ])

    # PR reference coverage against SO detail ids.
    ref_coverage = pd.DataFrame()
    if pr_ref_metadata is not None and not pr_ref_metadata.empty:
        rows = []
        so_keys = set(evidence["SO Detail Key"].dropna().astype(str))
        sample = pr_ref_sample.copy() if pr_ref_sample is not None else pd.DataFrame()
        for col in pr_ref_metadata["Column"].tolist():
            if sample.empty or col not in sample.columns:
                rows.append({"Column":col,"NonNull":0,"Unique":0,"Matches SO Detail":0,"Coverage of NonNull":0.0})
                continue
            keys = sample[col].map(_canonical_db_key)
            nonnull = keys.dropna()
            matches = nonnull.isin(so_keys)
            rows.append({
                "Column": col,
                "NonNull": int(nonnull.shape[0]),
                "Unique": int(nonnull.nunique()),
                "Matches SO Detail": int(matches.sum()),
                "Coverage of NonNull": float(matches.mean()) if len(matches) else 0.0,
            })
        ref_coverage = pd.DataFrame(rows).sort_values(["Matches SO Detail","Coverage of NonNull"], ascending=False)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        summary.to_excel(writer, index=False, sheet_name="SUMMARY")
        evidence.to_excel(writer, index=False, sheet_name="SO_ALL_EVIDENCE")
        pr_evidence.to_excel(writer, index=False, sheet_name="PR_DETAIL_EVIDENCE")
        pr_summary.to_excel(writer, index=False, sheet_name="PR_GROUP_SUMMARY")
        do_evidence.to_excel(writer, index=False, sheet_name="DO_DETAIL_EVIDENCE")
        bal.to_excel(writer, index=False, sheet_name="CURRENT_BALANCE")
        if pr_ref_metadata is not None:
            pr_ref_metadata.to_excel(writer, index=False, sheet_name="PR_REF_METADATA")
        if not ref_coverage.empty:
            ref_coverage.to_excel(writer, index=False, sheet_name="PR_REF_COVERAGE")
        if pr_ref_sample is not None and not pr_ref_sample.empty:
            pr_ref_sample.to_excel(writer, index=False, sheet_name="PR_REF_SAMPLE")
    return summary, output.getvalue()


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def probe_sales_pic_schema():
    """Cari kandidat kolom/FK yang mungkin menyimpan PIC Sales pada SO."""
    engine, _ = get_erp_database_connection()
    tables = ["x4_sales_order", "x4_sales_order_detail"]
    q_cols = text("""
        SELECT table_name, column_name, data_type, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ('x4_sales_order', 'x4_sales_order_detail')
        ORDER BY table_name, ordinal_position
    """)
    q_fk = text("""
        SELECT
            tc.table_name AS source_table,
            kcu.column_name AS source_column,
            ccu.table_name AS foreign_table,
            ccu.column_name AS foreign_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.constraint_schema = kcu.constraint_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.constraint_schema = tc.constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND tc.table_name IN ('x4_sales_order', 'x4_sales_order_detail')
        ORDER BY tc.table_name, kcu.column_name
    """)
    with engine.connect() as conn:
        cols = pd.read_sql_query(q_cols, conn)
        fks = pd.read_sql_query(q_fk, conn)

    tokens = ["sales", "pic", "employee", "user", "marketing", "owner", "assignee", "created_by", "updated_by"]
    if cols.empty:
        candidates = cols.copy()
    else:
        candidates = cols[
            cols["column_name"].astype(str).str.lower().apply(
                lambda x: any(token in x for token in tokens)
            )
        ].copy()

    sample = pd.DataFrame()
    if not candidates.empty:
        h_cols = candidates.loc[candidates["table_name"].eq("x4_sales_order"), "column_name"].tolist()
        if h_cols:
            safe_cols = ", ".join(_quote_ident(c) for c in h_cols[:20])
            q_sample = text(f"SELECT id, {safe_cols} FROM public.x4_sales_order ORDER BY id DESC LIMIT 50")
            try:
                with engine.connect() as conn:
                    sample = pd.read_sql_query(q_sample, conn)
            except Exception:
                sample = pd.DataFrame()
    return candidates, fks, sample


def build_pic_debug_excel(current_so: pd.DataFrame, candidates: pd.DataFrame | None = None, fks: pd.DataFrame | None = None, sample: pd.DataFrame | None = None) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        preview_cols = [c for c in ["transaction_number_so", "so_detail_id", "PIC Sales", "Status_so", "transaction_date"] if c in current_so.columns]
        current_so[preview_cols].drop_duplicates().to_excel(writer, index=False, sheet_name="CURRENT_PIC")
        if candidates is not None and not candidates.empty:
            candidates.to_excel(writer, index=False, sheet_name="CANDIDATE_COLUMNS")
        if fks is not None and not fks.empty:
            fks.to_excel(writer, index=False, sheet_name="FOREIGN_KEYS")
        if sample is not None and not sample.empty:
            sample.to_excel(writer, index=False, sheet_name="HEADER_SAMPLE")
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
# 6) DATABASE READING - API-FREE
# =========================================================
def _split_table_name(full_name: str) -> tuple[str, str]:
    if "." in full_name:
        schema, table = full_name.split(".", 1)
    else:
        schema, table = "public", full_name
    return schema, table


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def get_table_columns(full_name: str) -> list[str]:
    """Ambil nama kolom aktual dari PostgreSQL agar reader toleran terhadap variasi schema."""
    engine, _ = get_erp_database_connection()
    schema, table = _split_table_name(full_name)
    q = text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = :schema
          AND table_name = :table
        ORDER BY ordinal_position
    """)
    with engine.connect() as conn:
        rows = conn.execute(q, {"schema": schema, "table": table}).scalars().all()
    return [str(v) for v in rows]


def _first_existing(columns: list[str], candidates: list[str]) -> str | None:
    lookup = {str(c).lower(): c for c in columns}
    for candidate in candidates:
        if str(candidate).lower() in lookup:
            return lookup[str(candidate).lower()]
    return None



@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def discover_customer_master_table(
    transaction_header_table: str = "public.x4_sales_invoices",
    cache_version: str = CUSTOMER_LOOKUP_VERSION,
) -> dict | None:
    """
    Cari sumber nama customer dengan urutan yang lebih aman:

    1) Cek FOREIGN KEY kolom customer_id pada tabel transaksi.
       Ini paling akurat karena mengikuti relasi database sebenarnya.
    2) Jika FK tidak ada, cari tabel master yang namanya mengandung
       customer/client/partner/company/business dan mempunyai kolom ID + nama.
    3) Jika tetap tidak ditemukan, return None.

    `cache_version` sengaja menjadi bagian cache key agar perubahan mapping
    customer tidak tertahan cache Streamlit lama.
    """
    del cache_version  # hanya dipakai sebagai cache-busting key

    engine, _ = get_erp_database_connection()
    schema_name, table_name = _split_table_name(transaction_header_table)

    try:
        header_cols = get_table_columns(transaction_header_table)
    except Exception as exc:
        logger.warning(
            "Tidak dapat membaca kolom %s untuk discovery customer: %s",
            transaction_header_table,
            exc,
        )
        header_cols = []

    customer_fk_col = _first_existing(
        header_cols,
        [
            "customer_id",
            "client_id",
            "partner_id",
            "business_partner_id",
            "company_id",
            "customer",
        ],
    )

    name_candidates = [
        "customer_name",
        "company_name",
        "legal_name",
        "display_name",
        "full_name",
        "fullname",
        "partner_name",
        "client_name",
        "name",
        "title",
    ]

    # =========================================================
    # A. PRIORITAS 1: IKUTI FOREIGN KEY DARI SALES INVOICE
    # =========================================================
    if customer_fk_col:
        fk_query = text("""
            SELECT
                ccu.table_schema AS foreign_table_schema,
                ccu.table_name AS foreign_table_name,
                ccu.column_name AS foreign_column_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.constraint_schema = kcu.constraint_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = :schema_name
              AND tc.table_name = :table_name
              AND kcu.column_name = :customer_fk_col
            LIMIT 1
        """)

        try:
            with engine.connect() as conn:
                fk_row = conn.execute(
                    fk_query,
                    {
                        "schema_name": schema_name,
                        "table_name": table_name,
                        "customer_fk_col": customer_fk_col,
                    },
                ).mappings().first()
        except Exception as exc:
            logger.warning(
                "Gagal membaca FK customer pada %s.%s: %s",
                schema_name,
                table_name,
                exc,
            )
            fk_row = None

        if fk_row:
            master_table = (
                f"{fk_row['foreign_table_schema']}."
                f"{fk_row['foreign_table_name']}"
            )
            master_id_col = fk_row["foreign_column_name"]

            try:
                master_cols = get_table_columns(master_table)
            except Exception:
                master_cols = []

            master_name_col = _first_existing(
                master_cols,
                name_candidates,
            )

            if master_name_col:
                return {
                    "table": master_table,
                    "id_col": master_id_col,
                    "name_col": master_name_col,
                    "transaction_customer_col": customer_fk_col,
                    "discovery_method": "FOREIGN KEY",
                }

    # =========================================================
    # B. PRIORITAS 2: DISCOVERY TABEL MASTER RELEVAN
    # =========================================================
    table_query = text("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)

    try:
        with engine.connect() as conn:
            table_rows = conn.execute(table_query).scalars().all()
    except Exception as exc:
        logger.warning("Gagal membaca daftar tabel public: %s", exc)
        return None

    relevant_keywords = [
        "customer",
        "client",
        "partner",
        "company",
        "business",
        "account",
        "contact",
    ]

    preferred_tables = [
        "x4_customers",
        "x4_customer",
        "customers",
        "customer",
        "m_customer",
        "ms_customer",
        "master_customer",
        "customer_master",
        "business_partners",
        "business_partner",
        "x4_business_partners",
        "partners",
        "companies",
        "x4_companies",
        "contacts",
        "x4_contacts",
    ]

    ordered_tables = []

    for preferred in preferred_tables:
        if preferred in table_rows and preferred not in ordered_tables:
            ordered_tables.append(preferred)

    for table in table_rows:
        lower_table = str(table).lower()
        if (
            any(keyword in lower_table for keyword in relevant_keywords)
            and table not in ordered_tables
        ):
            ordered_tables.append(table)

    id_candidates = [
        "id",
        "customer_id",
        "client_id",
        "partner_id",
        "business_partner_id",
        "company_id",
    ]

    best_candidate = None
    best_score = -1

    for table in ordered_tables:
        full_table = f"public.{table}"

        try:
            cols = get_table_columns(full_table)
        except Exception:
            continue

        if not cols:
            continue

        id_col = _first_existing(cols, id_candidates)
        name_col = _first_existing(cols, name_candidates)

        if not id_col or not name_col or id_col == name_col:
            continue

        lower_table = str(table).lower()

        score = 0
        if "customer" in lower_table:
            score += 100
        if "client" in lower_table:
            score += 80
        if "business_partner" in lower_table or "businesspartner" in lower_table:
            score += 75
        if "partner" in lower_table:
            score += 60
        if "company" in lower_table:
            score += 50
        if "account" in lower_table:
            score += 30
        if "contact" in lower_table:
            score += 20

        if str(name_col).lower() == "customer_name":
            score += 40
        elif str(name_col).lower() == "company_name":
            score += 30
        elif str(name_col).lower() == "legal_name":
            score += 25
        elif str(name_col).lower() == "display_name":
            score += 20
        elif str(name_col).lower() == "name":
            score += 10

        if score > best_score:
            best_candidate = {
                "table": full_table,
                "id_col": id_col,
                "name_col": name_col,
                "transaction_customer_col": customer_fk_col,
                "discovery_method": "TABLE DISCOVERY",
            }
            best_score = score

    return best_candidate


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def load_customer_master(
    cache_version: str = CUSTOMER_LOOKUP_VERSION,
) -> pd.DataFrame:
    """
    Ambil mapping customer_id -> customer_name.

    Sumber utama mengikuti foreign key dari x4_sales_invoices.customer_id
    bila foreign key tersedia.
    """
    cfg = discover_customer_master_table(
        "public.x4_sales_invoices",
        cache_version=cache_version,
    )

    if not cfg:
        logger.warning(
            "Master customer tidak ditemukan. "
            "Customer masih akan fallback ke ID."
        )
        return pd.DataFrame(
            columns=[
                "customer_id",
                "customer_name",
                "customer_master_table",
            ]
        )

    engine, _ = get_erp_database_connection()

    table_name = cfg["table"]
    id_col = cfg["id_col"]
    name_col = cfg["name_col"]

    query = text(f"""
        SELECT
            {_quote_ident(id_col)}::text AS customer_id,
            {_quote_ident(name_col)}::text AS customer_name
        FROM {table_name}
        WHERE {_quote_ident(id_col)} IS NOT NULL
    """)

    try:
        with engine.connect() as conn:
            master = pd.read_sql_query(query, conn)
    except Exception as exc:
        logger.warning(
            "Gagal membaca customer master %s: %s",
            table_name,
            exc,
        )
        return pd.DataFrame(
            columns=[
                "customer_id",
                "customer_name",
                "customer_master_table",
            ]
        )

    if master.empty:
        return pd.DataFrame(
            columns=[
                "customer_id",
                "customer_name",
                "customer_master_table",
            ]
        )

    master["customer_id"] = (
        master["customer_id"]
        .map(_canonical_db_key)
    )

    master["customer_name"] = (
        master["customer_name"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    # Nama kosong / null / numeric murni tidak dianggap nama customer valid.
    invalid_name = (
        master["customer_name"].eq("")
        | master["customer_name"].str.lower().isin(
            ["nan", "none", "null", "<na>"]
        )
        | master["customer_name"].str.fullmatch(r"\d+(?:\.0+)?", na=False)
    )

    master = master[
        master["customer_id"].notna()
        & ~invalid_name
    ].copy()

    master["customer_master_table"] = table_name

    master = master.drop_duplicates(
        subset=["customer_id"],
        keep="first",
    )

    return master.reset_index(drop=True)


def _looks_like_customer_id(value) -> bool:
    """
    True jika nilai Customer tampak seperti ID:
    - kosong
    - angka murni
    - integer dengan .0
    """
    if value is None or pd.isna(value):
        return True

    raw = str(value).strip()

    if not raw or raw.lower() in {"nan", "none", "null", "<na>"}:
        return True

    return bool(re.fullmatch(r"\d+(?:\.0+)?", raw))


def enrich_customer_name(df: pd.DataFrame) -> pd.DataFrame:
    """
    Enrich dataframe transaksi sebelum rename kolom.

    Perbedaannya dengan versi sebelumnya:
    nama customer akan diganti dari master bukan hanya ketika blank,
    tetapi juga ketika nilai customer_name masih berupa angka ID.
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    if "customer_name" not in out.columns:
        out["customer_name"] = ""

    if "customer_id" not in out.columns:
        out["customer_id"] = ""

    out["customer_name"] = (
        out["customer_name"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    out["customer_id"] = (
        out["customer_id"]
        .map(_canonical_db_key)
    )

    master = load_customer_master()

    if not master.empty:
        customer_map = master.set_index("customer_id")["customer_name"]

        needs_lookup = out["customer_name"].map(_looks_like_customer_id)

        resolved_name = (
            out.loc[needs_lookup, "customer_id"]
            .map(customer_map)
            .fillna("")
        )

        has_resolved_name = resolved_name.ne("")

        resolved_index = resolved_name.index[has_resolved_name]

        out.loc[resolved_index, "customer_name"] = (
            resolved_name.loc[resolved_index]
        )

    # Fallback terakhir hanya jika benar-benar belum ada nama.
    blank_after_lookup = out["customer_name"].map(_looks_like_customer_id)

    out.loc[blank_after_lookup, "customer_name"] = (
        out.loc[blank_after_lookup, "customer_id"]
        .fillna("")
        .astype(str)
    )

    return out


def apply_customer_name_lookup(
    df: pd.DataFrame,
    id_col: str = "Customer ID",
    name_col: str = "Customer",
) -> pd.DataFrame:
    """
    Second-pass lookup setelah rename dataframe.

    Ini penting untuk:
    - memperbaiki dataframe hasil cache lama,
    - mengganti Customer yang masih terlihat seperti 997 / 650 / 795,
    - memastikan Pareto memakai nama customer.
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    if id_col not in out.columns:
        return out

    if name_col not in out.columns:
        out[name_col] = ""

    out[id_col] = out[id_col].map(_canonical_db_key)

    out[name_col] = (
        out[name_col]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    master = load_customer_master()

    if master.empty:
        return out

    customer_map = master.set_index("customer_id")["customer_name"]

    needs_lookup = (
        out[name_col].map(_looks_like_customer_id)
        | (
            out[name_col].map(_canonical_db_key)
            == out[id_col]
        )
    )

    resolved = (
        out.loc[needs_lookup, id_col]
        .map(customer_map)
        .fillna("")
    )

    valid = resolved.ne("")
    idx = resolved.index[valid]

    out.loc[idx, name_col] = resolved.loc[idx]

    return out


def customer_lookup_diagnostic(
    df: pd.DataFrame,
    id_col: str = "Customer ID",
    name_col: str = "Customer",
) -> dict:
    """
    Ringkasan diagnostic customer mapping untuk ditampilkan di footer.
    """
    result = {
        "total_rows": 0,
        "with_customer_id": 0,
        "resolved_name": 0,
        "still_id": 0,
    }

    if df is None or df.empty:
        return result

    result["total_rows"] = len(df)

    if id_col in df.columns:
        result["with_customer_id"] = int(
            df[id_col].notna().sum()
        )

    if name_col in df.columns:
        is_id = df[name_col].map(_looks_like_customer_id)
        result["still_id"] = int(is_id.sum())
        result["resolved_name"] = int((~is_id).sum())

    return result



# =========================================================
# CUSTOMER DEBUG / AUTO RESOLVE BERDASARKAN ID AKTUAL
# =========================================================

def _customer_name_is_valid(value) -> bool:
    """
    Nama customer valid jika bukan kosong/null dan bukan angka murni.
    """
    if value is None or pd.isna(value):
        return False

    raw = str(value).strip()

    if not raw or raw.lower() in {"nan", "none", "null", "<na>"}:
        return False

    # 3238 / 3238.0 dianggap ID, bukan nama.
    if re.fullmatch(r"\d+(?:\.0+)?", raw):
        return False

    return True


def _customer_id_candidates_from_columns(columns: list[str]) -> list[str]:
    """
    Urutkan kandidat kolom ID customer dari yang paling relevan.
    """
    priority = [
        "customer_id",
        "customerid",
        "client_id",
        "partner_id",
        "business_partner_id",
        "businesspartner_id",
        "company_id",
        "account_id",
        "contact_id",
        "id",
    ]

    lookup = {str(c).lower(): c for c in columns}
    result = []

    for candidate in priority:
        if candidate in lookup and lookup[candidate] not in result:
            result.append(lookup[candidate])

    # Tambahkan kolom lain yang berakhiran _id dan mengandung keyword customer-like.
    for col in columns:
        key = str(col).lower()
        if (
            key.endswith("_id")
            and any(
                token in key
                for token in [
                    "customer",
                    "client",
                    "partner",
                    "company",
                    "account",
                    "contact",
                    "business",
                ]
            )
            and col not in result
        ):
            result.append(col)

    return result


def _customer_name_candidates_from_columns(columns: list[str]) -> list[str]:
    """
    Urutkan kandidat nama customer.
    """
    priority = [
        "customer_name",
        "customername",
        "company_name",
        "legal_name",
        "display_name",
        "full_name",
        "fullname",
        "partner_name",
        "client_name",
        "business_name",
        "account_name",
        "contact_name",
        "name",
        "title",
    ]

    lookup = {str(c).lower(): c for c in columns}
    result = []

    for candidate in priority:
        if candidate in lookup and lookup[candidate] not in result:
            result.append(lookup[candidate])

    # Tambahkan kolom berakhiran _name yang relevan.
    for col in columns:
        key = str(col).lower()
        if (
            key.endswith("_name")
            and any(
                token in key
                for token in [
                    "customer",
                    "client",
                    "partner",
                    "company",
                    "account",
                    "contact",
                    "business",
                    "legal",
                    "display",
                ]
            )
            and col not in result
        ):
            result.append(col)

    return result


def _customer_candidate_semantic_score(
    table_name: str,
    id_col: str,
    name_col: str,
) -> int:
    """
    Score hanya untuk tie-breaker.
    Coverage ID aktual tetap menjadi faktor utama.
    """
    table_key = str(table_name).lower()
    id_key = str(id_col).lower()
    name_key = str(name_col).lower()

    score = 0

    table_weights = {
        "customer": 150,
        "client": 120,
        "business_partner": 110,
        "partner": 100,
        "company": 80,
        "account": 60,
        "contact": 40,
        "business": 30,
    }
    for token, weight in table_weights.items():
        if token in table_key:
            score += weight

    id_weights = {
        "customer_id": 150,
        "client_id": 120,
        "partner_id": 110,
        "business_partner_id": 105,
        "company_id": 80,
        "account_id": 60,
        "contact_id": 40,
        "id": 10,
    }
    score += id_weights.get(id_key, 0)

    name_weights = {
        "customer_name": 150,
        "company_name": 120,
        "legal_name": 110,
        "display_name": 100,
        "partner_name": 90,
        "client_name": 90,
        "business_name": 80,
        "account_name": 70,
        "full_name": 60,
        "fullname": 60,
        "name": 20,
        "title": 10,
    }
    score += name_weights.get(name_key, 0)

    return score


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def probe_customer_mapping_sources(
    customer_ids: tuple[str, ...],
    cache_version: str = CUSTOMER_LOOKUP_VERSION,
) -> pd.DataFrame:
    """
    DEBUG UTAMA.

    Mencari ID customer aktual ke seluruh tabel schema `public` yang memiliki:
      - kandidat kolom ID
      - kandidat kolom nama

    BUKAN menebak berdasarkan nama tabel saja.

    Output satu baris per mapping yang benar-benar ditemukan:
      customer_id -> candidate_name
    beserta sumber tabel/kolomnya.
    """
    del cache_version  # cache-busting key

    clean_ids = []
    for value in customer_ids:
        key = _canonical_db_key(value)
        if key is not None and key not in clean_ids:
            clean_ids.append(key)

    # Jaga query debug tetap ringan.
    clean_ids = clean_ids[:200]

    output_cols = [
        "customer_id",
        "candidate_name",
        "source_table",
        "id_column",
        "name_column",
        "semantic_score",
    ]

    if not clean_ids:
        return pd.DataFrame(columns=output_cols)

    engine, _ = get_erp_database_connection()

    # Ambil metadata seluruh kolom di public sekali saja.
    metadata_query = text("""
        SELECT
            table_name,
            column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    """)

    try:
        with engine.connect() as conn:
            metadata = pd.read_sql_query(metadata_query, conn)
    except Exception as exc:
        logger.warning("Customer probe gagal membaca metadata: %s", exc)
        return pd.DataFrame(columns=output_cols)

    if metadata.empty:
        return pd.DataFrame(columns=output_cols)

    table_to_cols = (
        metadata.groupby("table_name")["column_name"]
        .apply(list)
        .to_dict()
    )

    candidate_combinations = []

    for table_name, columns in table_to_cols.items():
        id_cols = _customer_id_candidates_from_columns(columns)
        name_cols = _customer_name_candidates_from_columns(columns)

        if not id_cols or not name_cols:
            continue

        # Batasi kombinasi per tabel agar tidak terlalu berat.
        for id_col in id_cols[:4]:
            for name_col in name_cols[:5]:
                if str(id_col).lower() == str(name_col).lower():
                    continue

                semantic_score = _customer_candidate_semantic_score(
                    table_name,
                    id_col,
                    name_col,
                )

                candidate_combinations.append(
                    (
                        semantic_score,
                        table_name,
                        id_col,
                        name_col,
                    )
                )

    # Semantik hanya menentukan urutan probe.
    candidate_combinations.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    # Maksimal 180 kombinasi. Biasanya jauh lebih sedikit.
    candidate_combinations = candidate_combinations[:180]

    placeholders = ", ".join(
        f":cid_{i}" for i in range(len(clean_ids))
    )
    params = {
        f"cid_{i}": customer_id
        for i, customer_id in enumerate(clean_ids)
    }

    hits = []

    for semantic_score, table_name, id_col, name_col in candidate_combinations:
        table_sql = f'public.{_quote_ident(table_name)}'
        id_sql = _quote_ident(id_col)
        name_sql = _quote_ident(name_col)

        q = text(f"""
            SELECT DISTINCT
                {id_sql}::text AS customer_id,
                {name_sql}::text AS candidate_name
            FROM {table_sql}
            WHERE {id_sql} IS NOT NULL
              AND {id_sql}::text IN ({placeholders})
              AND {name_sql} IS NOT NULL
            LIMIT 500
        """)

        try:
            with engine.connect() as conn:
                found = pd.read_sql_query(q, conn, params=params)
        except Exception:
            # Beberapa kolom/table dapat punya tipe khusus atau privilege terbatas.
            # Lewati dan lanjutkan probe sumber berikutnya.
            continue

        if found.empty:
            continue

        found["customer_id"] = found["customer_id"].map(_canonical_db_key)
        found["candidate_name"] = (
            found["candidate_name"]
            .fillna("")
            .astype(str)
            .str.strip()
        )

        found = found[
            found["customer_id"].notna()
            & found["candidate_name"].map(_customer_name_is_valid)
        ].copy()

        if found.empty:
            continue

        found["source_table"] = f"public.{table_name}"
        found["id_column"] = id_col
        found["name_column"] = name_col
        found["semantic_score"] = semantic_score

        hits.append(found[output_cols])

    if not hits:
        return pd.DataFrame(columns=output_cols)

    result = pd.concat(
        hits,
        ignore_index=True,
        sort=False,
    )

    return result.drop_duplicates().reset_index(drop=True)


def summarize_customer_probe(
    probe_df: pd.DataFrame,
    requested_ids: list[str] | tuple[str, ...],
) -> pd.DataFrame:
    """
    Ringkas sumber mapping customer.

    Source terbaik = source yang mencocokkan jumlah customer ID aktual terbanyak.
    Semantic score hanya dipakai sebagai tie-breaker.
    """
    output_cols = [
        "source_table",
        "id_column",
        "name_column",
        "IDs Matched",
        "Coverage",
        "Semantic Score",
        "Sample Mapping",
    ]

    if probe_df is None or probe_df.empty:
        return pd.DataFrame(columns=output_cols)

    requested = {
        _canonical_db_key(v)
        for v in requested_ids
        if _canonical_db_key(v) is not None
    }

    total_requested = max(len(requested), 1)

    summary_rows = []

    group_cols = [
        "source_table",
        "id_column",
        "name_column",
    ]

    for keys, group in probe_df.groupby(group_cols, dropna=False):
        source_table, id_column, name_column = keys

        matched_ids = group["customer_id"].dropna().unique().tolist()
        matched_count = len(matched_ids)

        samples = (
            group[["customer_id", "candidate_name"]]
            .drop_duplicates()
            .head(5)
        )
        sample_text = " | ".join(
            f"{row.customer_id} → {row.candidate_name}"
            for row in samples.itertuples(index=False)
        )

        summary_rows.append(
            {
                "source_table": source_table,
                "id_column": id_column,
                "name_column": name_column,
                "IDs Matched": matched_count,
                "Coverage": matched_count / total_requested,
                "Semantic Score": int(
                    pd.to_numeric(
                        group["semantic_score"],
                        errors="coerce",
                    ).fillna(0).max()
                ),
                "Sample Mapping": sample_text,
            }
        )

    summary = pd.DataFrame(summary_rows)

    if summary.empty:
        return pd.DataFrame(columns=output_cols)

    summary = summary.sort_values(
        ["IDs Matched", "Semantic Score"],
        ascending=[False, False],
    ).reset_index(drop=True)

    return summary[output_cols]


def get_best_customer_mapping_for_ids(
    customer_ids: list[str] | tuple[str, ...],
) -> tuple[pd.DataFrame, dict | None, pd.DataFrame]:
    """
    Return:
      mapping_df  -> customer_id, customer_name
      best_source -> metadata source terbaik
      summary_df  -> seluruh kandidat source untuk debug
    """
    clean_ids = []
    for value in customer_ids:
        key = _canonical_db_key(value)
        if key is not None and key not in clean_ids:
            clean_ids.append(key)

    if not clean_ids:
        return (
            pd.DataFrame(columns=["customer_id", "customer_name"]),
            None,
            pd.DataFrame(),
        )

    probe_df = probe_customer_mapping_sources(
        tuple(clean_ids),
        cache_version=CUSTOMER_LOOKUP_VERSION,
    )

    summary_df = summarize_customer_probe(
        probe_df,
        clean_ids,
    )

    if summary_df.empty:
        return (
            pd.DataFrame(columns=["customer_id", "customer_name"]),
            None,
            summary_df,
        )

    best = summary_df.iloc[0]

    best_mask = (
        probe_df["source_table"].eq(best["source_table"])
        & probe_df["id_column"].eq(best["id_column"])
        & probe_df["name_column"].eq(best["name_column"])
    )

    mapping = probe_df.loc[
        best_mask,
        ["customer_id", "candidate_name"],
    ].copy()

    # Bila satu ID punya beberapa nama, ambil nama non-numeric pertama.
    mapping = (
        mapping[
            mapping["candidate_name"].map(_customer_name_is_valid)
        ]
        .drop_duplicates(
            subset=["customer_id"],
            keep="first",
        )
        .rename(columns={"candidate_name": "customer_name"})
        .reset_index(drop=True)
    )

    best_source = {
        "table": best["source_table"],
        "id_col": best["id_column"],
        "name_col": best["name_column"],
        "ids_matched": int(best["IDs Matched"]),
        "coverage": float(best["Coverage"]),
        "semantic_score": int(best["Semantic Score"]),
        "method": "ACTUAL CUSTOMER ID PROBE",
    }

    return mapping, best_source, summary_df


def apply_customer_name_probe(
    df: pd.DataFrame,
    id_col: str = "Customer ID",
    name_col: str = "Customer",
) -> tuple[pd.DataFrame, dict]:
    """
    Resolver final yang dipakai sebelum Pareto.

    ID lookup diambil dari:
    1) Customer ID
    2) jika Customer ID kosong, nilai Customer yang masih berupa angka

    Setelah mapping ditemukan, angka Customer diganti dengan nama.
    """
    diagnostic = {
        "best_source": None,
        "summary": pd.DataFrame(),
        "requested_ids": [],
        "resolved_ids": [],
        "unresolved_ids": [],
    }

    if df is None or df.empty:
        return df, diagnostic

    out = df.copy()

    if name_col not in out.columns:
        out[name_col] = ""

    if id_col not in out.columns:
        out[id_col] = pd.NA

    out[name_col] = (
        out[name_col]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    out[id_col] = out[id_col].map(_canonical_db_key)

    # Bila Customer ID kosong namun Customer = "3238", jadikan 3238 lookup ID.
    numeric_name_id = out[name_col].apply(
        lambda x: (
            _canonical_db_key(x)
            if _looks_like_customer_id(x)
            else None
        )
    )

    lookup_id = out[id_col].copy()
    lookup_id = lookup_id.where(
        lookup_id.notna(),
        numeric_name_id,
    )

    requested_ids = [
        value
        for value in lookup_id.dropna().astype(str).unique().tolist()
        if value
    ]

    diagnostic["requested_ids"] = requested_ids

    if not requested_ids:
        return out, diagnostic

    mapping_df, best_source, summary_df = get_best_customer_mapping_for_ids(
        requested_ids
    )

    diagnostic["best_source"] = best_source
    diagnostic["summary"] = summary_df

    if mapping_df.empty:
        diagnostic["unresolved_ids"] = requested_ids
        return out, diagnostic

    customer_map = mapping_df.set_index("customer_id")["customer_name"]

    resolved_name = lookup_id.map(customer_map)

    needs_replace = (
        out[name_col].map(_looks_like_customer_id)
        & resolved_name.notna()
        & resolved_name.astype(str).str.strip().ne("")
    )

    out.loc[needs_replace, name_col] = (
        resolved_name.loc[needs_replace]
        .astype(str)
        .str.strip()
    )

    resolved_ids = mapping_df["customer_id"].dropna().astype(str).unique().tolist()
    diagnostic["resolved_ids"] = resolved_ids
    diagnostic["unresolved_ids"] = [
        customer_id
        for customer_id in requested_ids
        if customer_id not in set(resolved_ids)
    ]

    return out, diagnostic



def _select_expr(alias: str, column: str | None, output_alias: str, sql_type: str | None = None) -> str:
    """Bangun SELECT expression; bila source tidak ada tetap hasilkan kolom NULL."""
    out = _quote_ident(output_alias)
    if column:
        return f'{alias}.{_quote_ident(column)} AS {out}'
    if sql_type:
        return f'NULL::{sql_type} AS {out}'
    return f'NULL AS {out}'


def _coalesce_text_expr(
    first_alias: str, first_column: str | None,
    second_alias: str, second_column: str | None,
    output_alias: str,
) -> str:
    """COALESCE dua source text tanpa memaksa salah satu harus ada."""
    out = _quote_ident(output_alias)
    parts = []
    if first_column:
        parts.append(f'{first_alias}.{_quote_ident(first_column)}::text')
    if second_column:
        parts.append(f'{second_alias}.{_quote_ident(second_column)}::text')
    if not parts:
        return f'NULL::text AS {out}'
    if len(parts) == 1:
        return f'{parts[0]} AS {out}'
    return f'COALESCE({", ".join(parts)}) AS {out}'


# =========================================================
# STATUS COMPATIBILITY: DATABASE CODE -> LABEL API LAMA
# =========================================================
# Database ERP pada beberapa tabel menyimpan status sebagai kode numerik,
# sedangkan dashboard lama menerima `status_description` berbentuk teks dari API.
# Mapping ini sengaja mengembalikan vocabulary yang digunakan logic/grafik lama.
DB_STATUS_TO_API_LABEL = {
    0: "Draft",
    1: "Need Approve",
    2: "Approved",
    3: "In Progress",
    4: "Complete",
    # Pada beberapa modul ERP terdapat approval bertingkat. Dashboard lama hanya
    # mengenal satu bucket Approved, sehingga keduanya dinormalisasi ke Approved.
    5: "Approved",
    6: "Approved",
    7: "Close",
}

# SO Balance hanya menampilkan order yang masih outstanding.
# Sesuai business rule terbaru:
# - Draft        -> EXCLUDED
# - Need Approve -> EXCLUDED
# - Complete     -> EXCLUDED
SO_BALANCE_EXCLUDED_STATUSES = {"Draft", "Need Approve", "Complete"}


def normalize_api_status(value):
    """Ubah status DB ke label yang kompatibel dengan dashboard API lama."""
    if value is None or pd.isna(value):
        return ""

    raw = str(value).strip()
    if not raw or raw.lower() in {"nan", "none", "null", "<na>"}:
        return ""

    # Numeric status code: 3, '3', 3.0, dst.
    try:
        number = float(raw)
        if number.is_integer():
            code = int(number)
            if code in DB_STATUS_TO_API_LABEL:
                return DB_STATUS_TO_API_LABEL[code]
    except Exception:
        pass

    # Text status dari DB/API dinormalisasi ke vocabulary lama tanpa mengubah
    # meaning business-nya.
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


def normalize_status_column(df: pd.DataFrame, column: str) -> pd.DataFrame:
    out = df.copy()
    if column in out.columns:
        out[column] = out[column].map(normalize_api_status)
    return out


# =========================================================
# SOURCE PARITY HELPERS - POSTGRESQL vs ERP API
# =========================================================
@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def discover_fk_reference(full_table: str, source_column: str | None) -> dict | None:
    """Cari target FOREIGN KEY aktual untuk source_table.source_column."""
    if not source_column:
        return None
    engine, _ = get_erp_database_connection()
    schema_name, table_name = _split_table_name(full_table)
    q = text("""
        SELECT
            ccu.table_schema AS foreign_table_schema,
            ccu.table_name AS foreign_table_name,
            ccu.column_name AS foreign_column_name
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.constraint_schema = kcu.constraint_schema
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.constraint_schema = tc.constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = :schema_name
          AND tc.table_name = :table_name
          AND kcu.column_name = :source_column
        LIMIT 1
    """)
    try:
        with engine.connect() as conn:
            row = conn.execute(q, {
                "schema_name": schema_name,
                "table_name": table_name,
                "source_column": source_column,
            }).mappings().first()
    except Exception as exc:
        logger.warning("FK discovery gagal %s.%s: %s", full_table, source_column, exc)
        return None
    if not row:
        return None
    return {
        "table": f"{row['foreign_table_schema']}.{row['foreign_table_name']}",
        "id_col": row["foreign_column_name"],
    }


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def load_fk_value_map(
    source_table: str,
    source_column: str | None,
    value_candidates: tuple[str, ...],
) -> tuple[pd.DataFrame, dict | None]:
    """Load FK id -> requested display/code column dari master referensi."""
    ref = discover_fk_reference(source_table, source_column)
    if not ref:
        return pd.DataFrame(columns=["lookup_id", "lookup_value"]), None
    try:
        cols = get_table_columns(ref["table"])
    except Exception:
        return pd.DataFrame(columns=["lookup_id", "lookup_value"]), ref
    value_col = _first_existing(cols, list(value_candidates))
    if not value_col:
        return pd.DataFrame(columns=["lookup_id", "lookup_value"]), ref

    engine, _ = get_erp_database_connection()
    q = text(f"""
        SELECT
            {_quote_ident(ref['id_col'])}::text AS lookup_id,
            {_quote_ident(value_col)}::text AS lookup_value
        FROM {ref['table']}
        WHERE {_quote_ident(ref['id_col'])} IS NOT NULL
    """)
    try:
        with engine.connect() as conn:
            m = pd.read_sql_query(q, conn)
    except Exception as exc:
        logger.warning("FK map gagal %s: %s", ref["table"], exc)
        return pd.DataFrame(columns=["lookup_id", "lookup_value"]), ref

    if not m.empty:
        m["lookup_id"] = m["lookup_id"].map(_canonical_db_key)
        m["lookup_value"] = m["lookup_value"].fillna("").astype(str).str.strip()
        m = m[m["lookup_id"].notna() & m["lookup_value"].ne("")].drop_duplicates("lookup_id")
    meta = {**ref, "value_col": value_col}
    return m, meta


def _resolve_fk_series(
    series: pd.Series,
    source_table: str,
    source_column: str | None,
    value_candidates: tuple[str, ...],
) -> tuple[pd.Series, dict | None]:
    if series is None or not source_column:
        return series, None
    mapping, meta = load_fk_value_map(source_table, source_column, value_candidates)
    if mapping.empty:
        return series, meta
    lookup = mapping.set_index("lookup_id")["lookup_value"]
    keys = series.map(_canonical_db_key)
    resolved = keys.map(lookup)
    out = series.copy()
    valid = resolved.notna() & resolved.astype(str).str.strip().ne("")
    out.loc[valid] = resolved.loc[valid]
    return out, meta


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def probe_pic_name_mapping(
    pic_ids: tuple[str, ...],
    cache_version: str = "2026-09-23-sales-department-v1",
) -> tuple[pd.DataFrame, dict | None]:
    """
    Fallback bila `sales_department`/sales PIC tidak mempunyai FK constraint.

    Cari ID aktual ke master user/employee/staff/sales yang mempunyai kolom ID
    dan nama. Source dipilih berdasarkan coverage ID terbesar, semantic score
    hanya sebagai tie-breaker. Tidak menggunakan API dan tidak hard-code nama.
    """
    del cache_version

    clean_ids = []
    for value in pic_ids:
        key = _canonical_db_key(value)
        if key is not None and key not in clean_ids:
            clean_ids.append(key)
    clean_ids = clean_ids[:250]

    if not clean_ids:
        return pd.DataFrame(columns=["lookup_id", "lookup_value"]), None

    engine, _ = get_erp_database_connection()
    metadata_q = text("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    """)
    try:
        with engine.connect() as conn:
            metadata = pd.read_sql_query(metadata_q, conn)
    except Exception as exc:
        logger.warning("PIC probe metadata gagal: %s", exc)
        return pd.DataFrame(columns=["lookup_id", "lookup_value"]), None

    if metadata.empty:
        return pd.DataFrame(columns=["lookup_id", "lookup_value"]), None

    table_to_cols = metadata.groupby("table_name")["column_name"].apply(list).to_dict()

    table_keywords = {
        "user": 150,
        "employee": 145,
        "staff": 135,
        "sales": 125,
        "person": 110,
        "account": 80,
        "department": 60,
        "contact": 40,
    }
    transaction_tokens = [
        "sales_order", "invoice", "purchase", "delivery", "receipt",
        "detail", "history", "log", "audit",
    ]
    id_candidates = [
        "id", "user_id", "employee_id", "staff_id", "sales_id",
        "sales_person_id", "person_id", "account_id", "department_id",
        "sales_department",
    ]
    name_candidates = [
        "employee_name", "sales_name", "pic_name", "full_name", "fullname",
        "display_name", "name", "username", "user_name", "email",
    ]

    candidates = []
    for table_name, cols in table_to_cols.items():
        tkey = str(table_name).lower()
        if not any(k in tkey for k in table_keywords):
            continue
        if any(tok in tkey for tok in transaction_tokens):
            continue
        id_col = _first_existing(cols, id_candidates)
        name_col = _first_existing(cols, name_candidates)
        if not id_col or not name_col or id_col == name_col:
            continue
        score = sum(weight for token, weight in table_keywords.items() if token in tkey)
        if str(id_col).lower() != "id":
            score += 20
        if str(name_col).lower() in {"employee_name", "sales_name", "full_name", "display_name"}:
            score += 30
        candidates.append((score, table_name, id_col, name_col))

    candidates.sort(reverse=True)
    candidates = candidates[:120]

    placeholders = ", ".join(f":pid_{i}" for i in range(len(clean_ids)))
    params = {f"pid_{i}": v for i, v in enumerate(clean_ids)}
    source_hits = []

    for semantic_score, table_name, id_col, name_col in candidates:
        q = text(f"""
            SELECT DISTINCT
                {_quote_ident(id_col)}::text AS lookup_id,
                {_quote_ident(name_col)}::text AS lookup_value
            FROM public.{_quote_ident(table_name)}
            WHERE {_quote_ident(id_col)} IS NOT NULL
              AND {_quote_ident(id_col)}::text IN ({placeholders})
              AND {_quote_ident(name_col)} IS NOT NULL
            LIMIT 500
        """)
        try:
            with engine.connect() as conn:
                found = pd.read_sql_query(q, conn, params=params)
        except Exception:
            continue
        if found.empty:
            continue
        found["lookup_id"] = found["lookup_id"].map(_canonical_db_key)
        found["lookup_value"] = found["lookup_value"].fillna("").astype(str).str.strip()
        found = found[
            found["lookup_id"].notna()
            & found["lookup_value"].ne("")
            & ~found["lookup_value"].str.fullmatch(r"\d+(?:\.0+)?", na=False)
        ].drop_duplicates("lookup_id")
        if found.empty:
            continue
        source_hits.append({
            "matched": int(found["lookup_id"].nunique()),
            "semantic": semantic_score,
            "table": f"public.{table_name}",
            "id_col": id_col,
            "name_col": name_col,
            "mapping": found[["lookup_id", "lookup_value"]].copy(),
        })

    if not source_hits:
        return pd.DataFrame(columns=["lookup_id", "lookup_value"]), None

    source_hits.sort(key=lambda x: (x["matched"], x["semantic"]), reverse=True)
    best = source_hits[0]
    return best["mapping"], {
        "table": best["table"],
        "id_col": best["id_col"],
        "value_col": best["name_col"],
        "matched": best["matched"],
        "method": "ACTUAL PIC ID PROBE",
    }


def resolve_pic_series_with_fallback(
    series: pd.Series,
    source_table: str,
    source_column: str | None,
) -> tuple[pd.Series, dict | None]:
    """Resolve PIC via FK dahulu, lalu actual-ID probe bila masih numeric."""
    if series is None or not source_column:
        return series, None

    out, meta = _resolve_fk_series(
        series,
        source_table,
        source_column,
        (
            "employee_name", "sales_name", "pic_name", "full_name",
            "fullname", "display_name", "name", "username", "user_name", "email",
        ),
    )

    unresolved_mask = out.map(_looks_like_customer_id)
    unresolved_ids = [
        _canonical_db_key(v)
        for v in out.loc[unresolved_mask].tolist()
        if _canonical_db_key(v) is not None
    ]
    unresolved_ids = list(dict.fromkeys(unresolved_ids))

    if unresolved_ids:
        mapping, probe_meta = probe_pic_name_mapping(tuple(unresolved_ids))
        if not mapping.empty:
            lookup = mapping.set_index("lookup_id")["lookup_value"]
            keys = out.map(_canonical_db_key)
            resolved = keys.map(lookup)
            valid = unresolved_mask & resolved.notna() & resolved.astype(str).str.strip().ne("")
            out.loc[valid] = resolved.loc[valid]
            if probe_meta:
                meta = probe_meta

    return out, meta


def _soft_delete_sql_predicates(
    alias: str,
    columns: list[str],
    include_active_current: bool = True,
) -> list[str]:
    """
    Filter revision/deleted row berdasarkan metadata DB yang eksplisit.

    `include_active_current=False` dipakai untuk header transaksi agar kolom
    generic seperti `active` tidak salah membuang dokumen Complete/Close.
    Deleted flags tetap aman diterapkan pada header maupun detail.
    """
    lookup = {str(c).lower(): c for c in columns}
    predicates = []

    for candidate in ["deleted_at", "date_deleted", "deleted_date", "removed_at"]:
        if candidate in lookup:
            predicates.append(f'{alias}.{_quote_ident(lookup[candidate])} IS NULL')

    for candidate in ["is_deleted", "deleted", "is_removed"]:
        if candidate in lookup:
            col = _quote_ident(lookup[candidate])
            predicates.append(
                f"LOWER(COALESCE({alias}.{col}::text, 'false')) "
                "NOT IN ('true','t','1','yes','y','on')"
            )

    if include_active_current:
        for candidate in ["is_active", "active"]:
            if candidate in lookup:
                col = _quote_ident(lookup[candidate])
                predicates.append(
                    f"LOWER(COALESCE({alias}.{col}::text, 'true')) "
                    "IN ('true','t','1','yes','y','on')"
                )

        for candidate in ["is_current", "current", "is_latest", "latest"]:
            if candidate in lookup:
                col = _quote_ident(lookup[candidate])
                predicates.append(
                    f"LOWER(COALESCE({alias}.{col}::text, 'true')) "
                    "IN ('true','t','1','yes','y','on')"
                )

    return predicates


def _derive_tax_percentage_from_db_values(df: pd.DataFrame) -> pd.DataFrame:
    """
    Isi tax percentage jika kolom percentage kosong. Prioritas fallback:
    1) item_transaction_total dibanding taxable base;
    2) item_tax1_value dibanding taxable base.
    Tidak ada hard-code 11%.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    if "item_tax1_percentage" not in out.columns:
        out["item_tax1_percentage"] = 0.0

    qty = pd.to_numeric(out.get("item_quantity", 0), errors="coerce").fillna(0)
    price = pd.to_numeric(out.get("item_price", 0), errors="coerce").fillna(0)
    disc = pd.to_numeric(out.get("item_discount", 0), errors="coerce").fillna(0)
    direct_pct = pd.to_numeric(out["item_tax1_percentage"], errors="coerce").fillna(0)
    taxable_base = qty * (price - (price * disc / 100.0))

    derived = pd.Series(0.0, index=out.index, dtype="float64")

    if "item_transaction_total" in out.columns:
        line_total = pd.to_numeric(out["item_transaction_total"], errors="coerce").fillna(0)
        valid = (taxable_base > 0) & (line_total > 0)
        derived.loc[valid] = ((line_total.loc[valid] / taxable_base.loc[valid]) - 1.0) * 100.0

    if "item_tax1_value" in out.columns:
        tax_value = pd.to_numeric(out["item_tax1_value"], errors="coerce").fillna(0)
        valid_tax_value = (derived == 0) & (taxable_base > 0) & (tax_value > 0)
        derived.loc[valid_tax_value] = (tax_value.loc[valid_tax_value] / taxable_base.loc[valid_tax_value]) * 100.0

    derived = derived.where((derived >= 0) & (derived <= 100), 0).round(6)

    # Normalisasi precision hasil derivasi. Nilai seperti 10.999999 / 11.000001
    # merupakan artefak floating-point dari transaction_total, bukan rate pajak baru.
    # Jika sangat dekat dengan integer, snap ke integer agar parity dengan API stabil.
    nearest_integer = derived.round(0)
    near_integer = (derived - nearest_integer).abs() < 0.001
    derived.loc[near_integer] = nearest_integer.loc[near_integer]

    use = (direct_pct == 0) & (derived > 0)
    out.loc[use, "item_tax1_percentage"] = derived.loc[use]

    # Terapkan precision guard yang sama pada percentage langsung dari DB bila
    # source menyimpan nilai sangat dekat dengan integer.
    normalized_pct = pd.to_numeric(
        out["item_tax1_percentage"], errors="coerce"
    ).fillna(0.0)
    normalized_nearest = normalized_pct.round(0)
    normalized_near_integer = (
        normalized_pct - normalized_nearest
    ).abs() < 0.001
    normalized_pct.loc[normalized_near_integer] = (
        normalized_nearest.loc[normalized_near_integer]
    )
    out["item_tax1_percentage"] = normalized_pct

    out["tax_percentage_source"] = "DB_PERCENTAGE"
    out.loc[use, "tax_percentage_source"] = "DERIVED_FROM_DB_VALUE"
    return out


def _line_nominal_after_tax(df: pd.DataFrame) -> pd.Series:
    """Nilai line final after-tax, sedekat mungkin dengan item transaction_total API."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    idx = df.index
    qty = pd.to_numeric(df.get("item_quantity", pd.Series(0, index=idx)), errors="coerce").fillna(0)
    price = pd.to_numeric(df.get("item_price", pd.Series(0, index=idx)), errors="coerce").fillna(0)
    disc = pd.to_numeric(df.get("item_discount", pd.Series(0, index=idx)), errors="coerce").fillna(0)
    tax_pct = pd.to_numeric(df.get("item_tax1_percentage", pd.Series(0, index=idx)), errors="coerce").fillna(0)

    discount_unit = price * disc / 100.0
    taxable_unit = price - discount_unit
    computed = qty * (taxable_unit + taxable_unit * tax_pct / 100.0)
    result = computed.copy()

    # Fallback direct tax value jika ada.
    if "item_tax1_value" in df.columns:
        tax_value = pd.to_numeric(df["item_tax1_value"], errors="coerce").fillna(0)
        base = qty * taxable_unit
        via_tax_value = base + tax_value
        mask = (tax_value != 0) & (via_tax_value != 0)
        result.loc[mask] = via_tax_value.loc[mask]

    # Source paling kuat: transaction_total detail.
    if "item_transaction_total" in df.columns:
        direct = pd.to_numeric(df["item_transaction_total"], errors="coerce").fillna(0)
        mask = direct != 0
        result.loc[mask] = direct.loc[mask]

    return result.fillna(0)


def _reconciled_total_so_from_header(
    status_valid_df: pd.DataFrame,
    final_item_df: pd.DataFrame,
) -> tuple[float, float, float]:
    """
    Control Total SO menggunakan header transaction_total satu kali per SO.

    Ini setara dengan SUM current line API, tetapi kebal terhadap historical/revision
    detail yang masih tersimpan di tabel PostgreSQL. Keyword exclusion tetap dikurangkan.
    """
    if status_valid_df is None or status_valid_df.empty:
        return 0.0, 0.0, 0.0

    work = status_valid_df.copy()
    key = "header_id" if "header_id" in work.columns else "transaction_number_so"

    if "transaction_total" in work.columns:
        header_one = work.drop_duplicates(subset=[key], keep="last")
        gross = float(pd.to_numeric(header_one["transaction_total"], errors="coerce").fillna(0).sum())
    else:
        gross = float(_line_nominal_after_tax(work).sum())

    keyword_mask = pd.Series(False, index=work.index)
    if "item_name" in work.columns:
        keyword_mask = work["item_name"].astype(str).str.contains(
            r"Jasa|Biaya|Admin|Pengiriman", case=False, na=False
        )
    excluded_rows = work[keyword_mask].copy()

    # Apabila historical duplicate keyword ada, gunakan detail paling baru per
    # kombinasi SO + nama item + item internal + qty + price.
    if not excluded_rows.empty and "so_detail_id" in excluded_rows.columns:
        dedupe_cols = [c for c in [
            "transaction_number_so", "product_id", "item_name",
            "item_quantity", "item_price", "item_discount"
        ] if c in excluded_rows.columns]
        excluded_rows["__detail_num"] = pd.to_numeric(
            excluded_rows["so_detail_id"], errors="coerce"
        )
        excluded_rows = excluded_rows.sort_values("__detail_num").drop_duplicates(
            subset=dedupe_cols, keep="last"
        ) if dedupe_cols else excluded_rows

    excluded = float(_line_nominal_after_tax(excluded_rows).sum()) if not excluded_rows.empty else 0.0
    return gross - excluded, gross, excluded


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def read_stage_from_database(stage: str, start_date=None, end_date=None) -> pd.DataFrame:
    """
    Membaca header + detail langsung dari PostgreSQL dan mengeluarkan bentuk
    DataFrame yang kompatibel dengan hasil API baru lama.

    Penting:
    - item_id            = primary key detail (compat API)
    - item_product_id    = product/item identity dari detail.item_id/product_id
    - transaction_number = nomor dokumen header
    - transaction_date   = tanggal dokumen header
    """
    if stage not in DB_STAGES:
        raise KeyError(f"Stage database tidak dikenal: {stage}")

    cfg = DB_STAGES[stage]
    header_table = cfg["header_table"]
    detail_table = cfg["detail_table"]

    header_cols = get_table_columns(header_table)
    detail_cols = get_table_columns(detail_table)
    if not header_cols:
        raise RuntimeError(f"Tabel header tidak ditemukan / tidak terbaca: {header_table}")
    if not detail_cols:
        raise RuntimeError(f"Tabel detail tidak ditemukan / tidak terbaca: {detail_table}")

    header_id = _first_existing(header_cols, ["id"] + cfg.get("header_id_candidates", []))
    detail_header_fk = _first_existing(detail_cols, cfg["header_fk_candidates"])
    detail_id = _first_existing(detail_cols, cfg["detail_id_candidates"])
    product_id = _first_existing(detail_cols, cfg["product_candidates"])

    if not header_id or not detail_header_fk or not detail_id:
        raise RuntimeError(
            f"Mapping key {stage.upper()} belum lengkap. "
            f"header_id={header_id}, detail_header_fk={detail_header_fk}, detail_id={detail_id}"
        )

    tx_number = _first_existing(
        header_cols,
        ["transaction_number", "number", "document_number"],
    )
    header_date = _first_existing(header_cols, [cfg["header_date"], "transaction_date", "date"])
    status = _first_existing(
        header_cols,
        ["status_description", "status", "realization_status", "item_status_description"],
    )
    sales_pic = _first_existing(
        header_cols,
        [
            "pic_sales_name", "sales_pic_name", "sales_name", "sales_person_name",
            "sales_person", "pic_sales", "pic_sales_id", "sales_id",
            "sales_person_id", "salesman_id", "marketing_id",
            "sales_department", "sales_department_id",
        ],
    )
    if not sales_pic:
        # Fallback aman: hanya kolom *_id yang eksplisit mengandung kata sales.
        dynamic_sales_ids = [
            c for c in header_cols
            if str(c).lower().endswith("_id") and "sales" in str(c).lower()
        ]
        sales_pic = dynamic_sales_ids[0] if dynamic_sales_ids else None
    # Customer name dan customer ID dipisahkan.
    # Jangan alias customer_id sebagai customer_name karena akan membuat
    # Pareto Customer menampilkan angka ID (contoh: 997, 650, dst.).
    customer_name_col = _first_existing(
        header_cols,
        ["customer_name", "client_name", "partner_name", "company_name"],
    )
    customer_id_col = _first_existing(
        header_cols,
        ["customer_id", "customer", "client_id", "partner_id"],
    )
    vendor = _first_existing(
        header_cols,
        ["vendor_name", "supplier_name", "supplier", "vendor", "vendor_id", "supplier_id"],
    )
    header_pic_procurement = _first_existing(
        header_cols,
        ["item_pic_procurement_name", "pic_procurement_name", "pic_procurement_id", "pic_name"],
    )
    header_total = _first_existing(
        header_cols,
        ["transaction_total", "grand_total", "total_amount", "net_total", "total"],
    )

    date_approved = _first_existing(header_cols, ["date_approved", "approved_date", "approved_at"])
    date_inprogress = _first_existing(header_cols, ["date_inprogress", "inprogress_date", "in_progress_date", "in_progress_at"])
    date_complete = _first_existing(header_cols, ["date_complete", "complete_date", "completed_date", "completed_at"])

    item_name = _first_existing(detail_cols, cfg.get("item_name_candidates", ["item_name", "product_name", "name"]))
    item_price = _first_existing(detail_cols, ["price", "unit_price", "selling_price", "sales_price", "unit_value", "rate"])
    item_discount = _first_existing(detail_cols, ["discount", "discount_percentage", "discount_percent", "disc"])
    item_quantity = _first_existing(detail_cols, ["quantity", "item_quantity", "qty", "ordered_quantity", "invoice_quantity"])
    item_tax1_pct = _first_existing(detail_cols, ["tax1_percentage", "tax_1_percentage", "tax_percentage", "vat_percentage", "ppn_percentage"])
    item_tax1_value = _first_existing(detail_cols, ["tax1_value", "tax_1_value", "tax_value", "vat_value", "ppn_value"])
    item_tax2_pct = _first_existing(detail_cols, ["tax2_percentage", "tax_2_percentage"])
    item_transaction_total = _first_existing(
        detail_cols,
        ["transaction_total", "item_transaction_total", "line_transaction_total", "grand_total", "net_total"],
    )
    item_subtotal = _first_existing(
        detail_cols,
        ["sub_total", "subtotal", "line_total", "item_total", "net_amount", "total_amount", "amount", "total_value"],
    )

    # Quantity progress yang disimpan langsung pada detail transaksi.
    # Khusus SO, field ini sangat penting untuk SO Balance karena sebagian DO
    # tidak selalu dapat ditelusuri hanya dari DO.so_detail_id.
    item_do_quantity = _first_existing(
        detail_cols,
        ["do_quantity", "delivered_quantity", "delivery_quantity", "qty_do"],
    )
    item_si_quantity = _first_existing(
        detail_cols,
        ["si_quantity", "invoice_quantity", "invoiced_quantity", "qty_si"],
    )
    item_realized_quantity = _first_existing(
        detail_cols,
        ["realized_quantity", "realisation_quantity", "realization_quantity"],
    )
    item_do_status = _first_existing(
        detail_cols,
        ["do_status", "delivery_status"],
    )
    item_si_status = _first_existing(
        detail_cols,
        ["si_status", "invoice_status"],
    )
    detail_pic_procurement = _first_existing(
        detail_cols,
        ["item_pic_procurement_name", "pic_procurement_name", "pic_procurement_id"],
    )
    detail_sales_pic = _first_existing(
        detail_cols,
        [
            "pic_sales_name", "sales_pic_name", "sales_name", "sales_person_name",
            "sales_person", "pic_sales", "pic_sales_id", "sales_id",
            "sales_person_id", "salesman_id", "marketing_id",
            "sales_department", "sales_department_id",
        ],
    )
    detail_vendor = _first_existing(
        detail_cols,
        ["vendor_name", "supplier_name", "supplier", "vendor", "vendor_id", "supplier_id"],
    )

    ref_so = _first_existing(detail_cols, cfg.get("ref_so_candidates", []))
    ref_pr = _first_existing(detail_cols, cfg.get("ref_pr_candidates", []))
    ref_po = _first_existing(detail_cols, cfg.get("ref_po_candidates", []))
    ref_grn = _first_existing(detail_cols, cfg.get("ref_grn_candidates", []))
    ref_do = _first_existing(detail_cols, cfg.get("ref_do_candidates", []))

    # Phase 4: beberapa SI tidak mempunyai do_detail_id, tetapi API tetap membawa
    # so_transaction_number. Cari relasi langsung SI -> SO tanpa hard-code schema.
    detail_so_tx_number = _first_existing(
        detail_cols,
        [
            "so_transaction_number", "sales_order_transaction_number",
            "so_number", "sales_order_number", "source_so_number",
        ],
    )
    header_so_tx_number = _first_existing(
        header_cols,
        [
            "so_transaction_number", "sales_order_transaction_number",
            "so_number", "sales_order_number", "source_so_number",
        ],
    )
    detail_so_header_id = _first_existing(
        detail_cols,
        ["sales_order_id", "so_id", "source_sales_order_id", "source_so_id"],
    )
    header_so_header_id = _first_existing(
        header_cols,
        ["sales_order_id", "so_id", "source_sales_order_id", "source_so_id"],
    )

    # Header/item aliases sengaja meniru payload API lama agar seluruh logic dashboard
    # setelah bagian LOAD DATA tidak perlu ditulis ulang besar-besaran.
    select_parts = [
        _select_expr("h", header_id, "header_id"),
        _select_expr("h", tx_number, "transaction_number", "text"),
        _select_expr("h", header_date, "transaction_date", "timestamp"),
        _select_expr("h", status, "status_description", "text"),
        _coalesce_text_expr("d", detail_sales_pic, "h", sales_pic, "pic_sales_name"),
        _select_expr("h", customer_name_col, "customer_name", "text"),
        _select_expr("h", customer_id_col, "customer_id", "text"),
        _coalesce_text_expr("d", detail_vendor, "h", vendor, "vendor_name"),
        _select_expr("h", header_total, "transaction_total", "numeric"),
        _select_expr("h", date_approved, "date_approved", "timestamp"),
        _select_expr("h", date_inprogress, "date_inprogress", "timestamp"),
        _select_expr("h", date_complete, "date_complete", "timestamp"),
        _select_expr("d", detail_id, "item_id"),
        _select_expr("d", product_id, "item_product_id"),
        _select_expr("d", item_name, "item_item_name", "text"),
        _select_expr("d", item_price, "item_price", "numeric"),
        _select_expr("d", item_discount, "item_discount", "numeric"),
        _select_expr("d", item_quantity, "item_quantity", "numeric"),
        _select_expr("d", item_tax1_pct, "item_tax1_percentage", "numeric"),
        _select_expr("d", item_tax1_value, "item_tax1_value", "numeric"),
        _select_expr("d", item_tax2_pct, "item_tax2_percentage", "numeric"),
        _select_expr("d", item_subtotal, "item_sub_total", "numeric"),
        _select_expr("d", item_transaction_total, "item_transaction_total", "numeric"),
        _select_expr("d", item_do_quantity, "item_do_quantity", "numeric"),
        _select_expr("d", item_si_quantity, "item_si_quantity", "numeric"),
        _select_expr("d", item_realized_quantity, "item_realized_quantity", "numeric"),
        _select_expr("d", item_do_status, "item_do_status", "text"),
        _select_expr("d", item_si_status, "item_si_status", "text"),
        _select_expr("d", ref_so, "item_so_detail_id"),
        _select_expr("d", ref_pr, "item_pr_detail_id"),
        _select_expr("d", ref_po, "item_po_detail_id"),
        _select_expr("d", ref_grn, "item_grn_detail_id"),
        _select_expr("d", ref_do, "item_do_detail_id"),
        _coalesce_text_expr(
            "d", detail_so_tx_number,
            "h", header_so_tx_number,
            "item_so_transaction_number",
        ),
        _coalesce_text_expr(
            "d", detail_so_header_id,
            "h", header_so_header_id,
            "item_so_header_id",
        ),
    ]

    # PIC Procurement: detail lebih prioritas, header menjadi fallback.
    select_parts.append(
        _coalesce_text_expr(
            "d", detail_pic_procurement,
            "h", header_pic_procurement,
            "item_pic_procurement_name",
        )
    )

    params = {}
    where_parts = []
    if header_date and end_date is not None:
        params["next_date"] = pd.Timestamp(end_date) + pd.Timedelta(days=1)
        where_parts.append(f'h.{_quote_ident(header_date)} < :next_date')
    if header_date and start_date is not None:
        params["start_date"] = pd.Timestamp(start_date)
        where_parts.append(f'h.{_quote_ident(header_date)} >= :start_date')

    # API hanya mengembalikan row current/non-deleted. Terapkan rule yang sama
    # pada SELURUH stage (SO/PR/PO/GRN/DO/SI). Header hanya memakai deletion
    # flags yang aman; detail juga boleh memakai active/current flags eksplisit.
    where_parts.extend(
        _soft_delete_sql_predicates(
            "h", header_cols, include_active_current=False
        )
    )
    where_parts.extend(
        _soft_delete_sql_predicates(
            "d", detail_cols, include_active_current=True
        )
    )

    where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    query = text(f"""
        SELECT
            {', '.join(select_parts)}
        FROM {header_table} h
        JOIN {detail_table} d
          ON h.{_quote_ident(header_id)} = d.{_quote_ident(detail_header_fk)}
        {where_sql}
    """)

    engine, _ = get_erp_database_connection()
    with engine.connect() as conn:
        df = pd.read_sql_query(query, conn, params=params)

    df = df.loc[:, ~df.columns.duplicated()].copy()

    # ---------------------------------------------------------
    # PIC SALES: ID -> NAME melalui FK aktual bila tersedia
    # ---------------------------------------------------------
    pic_fk_meta = None
    if "pic_sales_name" in df.columns:
        # Detail lebih prioritas karena SELECT memakai COALESCE(detail, header).
        pic_source_table = detail_table if detail_sales_pic else header_table
        pic_source_col = detail_sales_pic if detail_sales_pic else sales_pic
        df["pic_sales_name"], pic_fk_meta = resolve_pic_series_with_fallback(
            df["pic_sales_name"],
            pic_source_table,
            pic_source_col,
        )

    # ---------------------------------------------------------
    # PRODUCT CODE API-LIKE: internal item_id tetap dipakai untuk join,
    # external code disediakan pada item_product_code.
    # ---------------------------------------------------------
    product_fk_meta = None
    df["item_product_code"] = pd.NA
    if product_id and "item_product_id" in df.columns:
        product_map, product_fk_meta = load_fk_value_map(
            detail_table,
            product_id,
            (
                "product_id", "product_code", "item_code", "item_number",
                "sku", "code", "part_number",
            ),
        )
        if not product_map.empty:
            product_lookup = product_map.set_index("lookup_id")["lookup_value"]
            df["item_product_code"] = (
                df["item_product_id"].map(_canonical_db_key).map(product_lookup)
            )

    # Ubah customer_id menjadi nama customer bila master customer tersedia.
    # Ini membuat Pareto / Concentration / Retention menggunakan nama,
    # bukan angka customer ID.
    df = enrich_customer_name(df)

    # Simpan raw status untuk diagnostic, lalu bentuk `status_description` seperti
    # payload API lama. Ini memperbaiki filter Total SO, Revenue, Pareto, pie chart, dll.
    if "status_description" in df.columns:
        df["status_raw_db"] = df["status_description"]
        df["status_description"] = df["status_description"].map(normalize_api_status)

    # Rekonsiliasi tax dari transaction_total/tax value detail jika percentage
    # tidak tersedia langsung di schema.
    df = _derive_tax_percentage_from_db_values(df)

    df = safe_to_datetime(df, "transaction_date")
    for col in ["date_approved", "date_inprogress", "date_complete"]:
        df = safe_to_datetime(df, col)

    # Persist mapping metadata in columns (attrs dapat hilang setelah copy/rename).
    df["reader_pic_source"] = (
        f"{(detail_table if detail_sales_pic else header_table)}."
        f"{(detail_sales_pic if detail_sales_pic else sales_pic) or '-'}"
    )
    df["reader_pic_master"] = (
        pic_fk_meta.get("table", "-") if pic_fk_meta else "-"
    )
    df["reader_product_source"] = f"{detail_table}.{product_id or '-'}"
    df["reader_product_master"] = (
        product_fk_meta.get("table", "-") if product_fk_meta else "-"
    )
    df["reader_line_transaction_total_col"] = item_transaction_total or "-"
    df["reader_so_transaction_number_source"] = (
        f"{detail_table}.{detail_so_tx_number}" if detail_so_tx_number
        else (f"{header_table}.{header_so_tx_number}" if header_so_tx_number else "-")
    )
    df["reader_so_header_id_source"] = (
        f"{detail_table}.{detail_so_header_id}" if detail_so_header_id
        else (f"{header_table}.{header_so_header_id}" if header_so_header_id else "-")
    )
    reader_delete_rules = (
        _soft_delete_sql_predicates(
            "h", header_cols, include_active_current=False
        )
        + _soft_delete_sql_predicates(
            "d", detail_cols, include_active_current=True
        )
    )
    df["reader_soft_delete_rule"] = (
        " | ".join(reader_delete_rules)
        or "NO EXPLICIT SOFT-DELETE/CURRENT COLUMN FOUND"
    )
    return df


def _enrich_si_tax_from_chain(
    si_df: pd.DataFrame,
    do_df: pd.DataFrame,
    so_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Lengkapi Tax1 % Sales Invoice tanpa hard-code rate.

    Prioritas Phase 4:
      1) tax SI sendiri bila > 0;
      2) tax DO berdasarkan SI.do_detail_id;
      3) tax SO melalui DO.so_detail_id;
      4) tax SO langsung berdasarkan SI.so_transaction_number + product/item;
      5) tax SO langsung berdasarkan SI.so_header_id + product/item.

    Nilai DB 0% diperlakukan sebagai "belum ter-resolve" bila source upstream
    mempunyai rate > 0. Ini diperlukan karena hasil rekonsiliasi API menunjukkan
    SI tertentu menyimpan 0/NULL tax pada detail walaupun SO sumber mempunyai PPN.
    """
    if si_df is None or si_df.empty:
        return si_df

    out = si_df.copy()
    if "item_tax1_percentage" not in out.columns:
        out["item_tax1_percentage"] = 0.0
    if "tax_percentage_source" not in out.columns:
        out["tax_percentage_source"] = "DB_PERCENTAGE"

    out["__do_key"] = out.get(
        "item_do_detail_id", pd.Series(index=out.index, dtype="object")
    ).map(_canonical_db_key)
    out["__product_key"] = out.get(
        "item_product_id", pd.Series(index=out.index, dtype="object")
    ).map(_canonical_db_key)
    out["__item_name_key"] = (
        out.get("item_item_name", pd.Series("", index=out.index, dtype="object"))
        .fillna("").astype(str).str.strip().str.lower()
    )
    out["__so_doc_key"] = (
        out.get("item_so_transaction_number", pd.Series("", index=out.index, dtype="object"))
        .fillna("").astype(str).str.strip()
    )
    out["__so_header_key"] = out.get(
        "item_so_header_id", pd.Series(index=out.index, dtype="object")
    ).map(_canonical_db_key)

    current_tax = pd.to_numeric(
        out["item_tax1_percentage"], errors="coerce"
    ).fillna(0.0)

    do = do_df.copy() if do_df is not None else pd.DataFrame()
    so = so_df.copy() if so_df is not None else pd.DataFrame()

    do_tax_strict = {}
    do_tax_detail = {}
    do_to_so_strict = {}
    do_to_so_detail = {}

    if not do.empty:
        do["__do_key"] = do.get(
            "item_id", pd.Series(index=do.index, dtype="object")
        ).map(_canonical_db_key)
        do["__product_key"] = do.get(
            "item_product_id", pd.Series(index=do.index, dtype="object")
        ).map(_canonical_db_key)
        do["__so_key"] = do.get(
            "item_so_detail_id", pd.Series(index=do.index, dtype="object")
        ).map(_canonical_db_key)
        do["__tax"] = pd.to_numeric(
            do.get("item_tax1_percentage", 0), errors="coerce"
        ).fillna(0.0)

        for row in do[["__do_key", "__product_key", "__so_key", "__tax"]].itertuples(index=False, name=None):
            do_key, product_key, so_key, tax = row
            if do_key is None:
                continue
            strict_key = (do_key, product_key)
            if tax and float(tax) > 0:
                do_tax_strict[strict_key] = max(
                    float(tax), do_tax_strict.get(strict_key, 0.0)
                )
                do_tax_detail[do_key] = max(
                    float(tax), do_tax_detail.get(do_key, 0.0)
                )
            if so_key is not None:
                do_to_so_strict.setdefault(strict_key, so_key)
                do_to_so_detail.setdefault(do_key, so_key)

    do_tax = []
    linked_so_keys = []
    for do_key, product_key in zip(out["__do_key"], out["__product_key"]):
        strict_key = (do_key, product_key)
        do_tax.append(
            do_tax_strict.get(strict_key, do_tax_detail.get(do_key, 0.0))
            if do_key is not None else 0.0
        )
        linked_so_keys.append(
            do_to_so_strict.get(strict_key, do_to_so_detail.get(do_key))
            if do_key is not None else None
        )

    do_tax = pd.Series(do_tax, index=out.index, dtype="float64")
    linked_so_keys = pd.Series(linked_so_keys, index=out.index, dtype="object")

    use_do = (current_tax <= 0) & (do_tax > 0)
    out.loc[use_do, "item_tax1_percentage"] = do_tax.loc[use_do]
    out.loc[use_do, "tax_percentage_source"] = "DO_DETAIL_FALLBACK"

    # Build SO tax lookup maps once. These maps support DO-chain and direct SI->SO.
    so_tax_strict = {}
    so_tax_detail = {}
    so_tax_doc_product = {}
    so_tax_doc_item = {}
    so_tax_header_product = {}
    so_tax_header_item = {}

    if not so.empty:
        so["__so_key"] = so.get(
            "item_id", pd.Series(index=so.index, dtype="object")
        ).map(_canonical_db_key)
        so["__product_key"] = so.get(
            "item_product_id", pd.Series(index=so.index, dtype="object")
        ).map(_canonical_db_key)
        so["__item_name_key"] = (
            so.get("item_item_name", pd.Series("", index=so.index, dtype="object"))
            .fillna("").astype(str).str.strip().str.lower()
        )
        so["__so_doc_key"] = (
            so.get("transaction_number", pd.Series("", index=so.index, dtype="object"))
            .fillna("").astype(str).str.strip()
        )
        so["__so_header_key"] = so.get(
            "header_id", pd.Series(index=so.index, dtype="object")
        ).map(_canonical_db_key)
        so["__tax"] = pd.to_numeric(
            so.get("item_tax1_percentage", 0), errors="coerce"
        ).fillna(0.0)

        for so_key, product_key, item_name_key, so_doc_key, so_header_key, tax in so[
            ["__so_key", "__product_key", "__item_name_key", "__so_doc_key", "__so_header_key", "__tax"]
        ].itertuples(index=False, name=None):
            if not tax or float(tax) <= 0:
                continue
            tax = float(tax)
            if so_key is not None:
                strict_key = (so_key, product_key)
                so_tax_strict[strict_key] = max(tax, so_tax_strict.get(strict_key, 0.0))
                so_tax_detail[so_key] = max(tax, so_tax_detail.get(so_key, 0.0))
            if so_doc_key:
                if product_key is not None:
                    k = (so_doc_key, product_key)
                    so_tax_doc_product[k] = max(tax, so_tax_doc_product.get(k, 0.0))
                if item_name_key:
                    k = (so_doc_key, item_name_key)
                    so_tax_doc_item[k] = max(tax, so_tax_doc_item.get(k, 0.0))
            if so_header_key is not None:
                if product_key is not None:
                    k = (so_header_key, product_key)
                    so_tax_header_product[k] = max(tax, so_tax_header_product.get(k, 0.0))
                if item_name_key:
                    k = (so_header_key, item_name_key)
                    so_tax_header_item[k] = max(tax, so_tax_header_item.get(k, 0.0))

        # 3) SO fallback via DO -> SO detail.
        so_tax = []
        for so_key, product_key in zip(linked_so_keys, out["__product_key"]):
            strict_key = (so_key, product_key)
            so_tax.append(
                so_tax_strict.get(strict_key, so_tax_detail.get(so_key, 0.0))
                if so_key is not None else 0.0
            )
        so_tax = pd.Series(so_tax, index=out.index, dtype="float64")

        refreshed = pd.to_numeric(
            out["item_tax1_percentage"], errors="coerce"
        ).fillna(0.0)
        use_so = (refreshed <= 0) & (so_tax > 0)
        out.loc[use_so, "item_tax1_percentage"] = so_tax.loc[use_so]
        out.loc[use_so, "tax_percentage_source"] = "SO_DETAIL_FALLBACK_VIA_DO"

        # 4) Direct SI -> SO by SO transaction number.
        direct_doc_tax = []
        for so_doc, product_key, item_name_key in zip(
            out["__so_doc_key"], out["__product_key"], out["__item_name_key"]
        ):
            value = 0.0
            if so_doc:
                if product_key is not None:
                    value = so_tax_doc_product.get((so_doc, product_key), 0.0)
                if value <= 0 and item_name_key:
                    value = so_tax_doc_item.get((so_doc, item_name_key), 0.0)
            direct_doc_tax.append(value)
        direct_doc_tax = pd.Series(direct_doc_tax, index=out.index, dtype="float64")
        refreshed = pd.to_numeric(out["item_tax1_percentage"], errors="coerce").fillna(0.0)
        use_direct_doc = (refreshed <= 0) & (direct_doc_tax > 0)
        out.loc[use_direct_doc, "item_tax1_percentage"] = direct_doc_tax.loc[use_direct_doc]
        out.loc[use_direct_doc, "tax_percentage_source"] = "SO_DETAIL_FALLBACK_VIA_SO_NUMBER"

        # 5) Direct SI -> SO by SO header id if schema exposes it.
        direct_header_tax = []
        for so_header, product_key, item_name_key in zip(
            out["__so_header_key"], out["__product_key"], out["__item_name_key"]
        ):
            value = 0.0
            if so_header is not None:
                if product_key is not None:
                    value = so_tax_header_product.get((so_header, product_key), 0.0)
                if value <= 0 and item_name_key:
                    value = so_tax_header_item.get((so_header, item_name_key), 0.0)
            direct_header_tax.append(value)
        direct_header_tax = pd.Series(direct_header_tax, index=out.index, dtype="float64")
        refreshed = pd.to_numeric(out["item_tax1_percentage"], errors="coerce").fillna(0.0)
        use_direct_header = (refreshed <= 0) & (direct_header_tax > 0)
        out.loc[use_direct_header, "item_tax1_percentage"] = direct_header_tax.loc[use_direct_header]
        out.loc[use_direct_header, "tax_percentage_source"] = "SO_DETAIL_FALLBACK_VIA_SO_HEADER"

    # 6) SI HEADER EFFECTIVE TAX FALLBACK.
    # Dipakai hanya untuk row yang masih 0% setelah seluruh chain DO/SO dicoba.
    # Rate tidak di-hardcode. Rate dihitung dari:
    #   header transaction_total / SUM(pre-tax line) - 1
    # Hasil forensic Phase 4 menunjukkan SI yang unresolved memiliki effective rate
    # header yang konsisten dengan API.
    refreshed = pd.to_numeric(
        out["item_tax1_percentage"], errors="coerce"
    ).fillna(0.0)
    out["header_effective_tax_pct"] = 0.0

    if (refreshed <= 0).any() and "transaction_total" in out.columns:
        qty = pd.to_numeric(out.get("item_quantity", 0), errors="coerce").fillna(0.0)
        price = pd.to_numeric(out.get("item_price", 0), errors="coerce").fillna(0.0)
        disc = pd.to_numeric(out.get("item_discount", 0), errors="coerce").fillna(0.0)
        pre_tax_line = qty * (price - (price * disc / 100.0))

        if "header_id" in out.columns:
            group_key = out["header_id"].map(_canonical_db_key)
        else:
            group_key = (
                out.get("transaction_number", pd.Series("", index=out.index))
                .fillna("").astype(str).str.strip()
            )

        tmp_tax = pd.DataFrame({
            "__group": group_key,
            "__pre_tax": pre_tax_line,
            "__header_total": pd.to_numeric(
                out.get("transaction_total", 0), errors="coerce"
            ).fillna(0.0),
        }, index=out.index)

        valid_group = tmp_tax["__group"].notna() & tmp_tax["__group"].astype(str).ne("")
        tmp_valid = tmp_tax.loc[valid_group].copy()
        if not tmp_valid.empty:
            group_base = tmp_valid.groupby("__group")["__pre_tax"].sum()
            group_header = tmp_valid.groupby("__group")["__header_total"].max()
            effective = ((group_header / group_base.replace(0, pd.NA)) - 1.0) * 100.0
            effective = pd.to_numeric(effective, errors="coerce").fillna(0.0)
            effective = effective.where((effective > 0) & (effective <= 100), 0.0)
            effective = effective.round(6)
            nearest = effective.round(0)
            near_integer = (effective - nearest).abs() < 0.001
            effective.loc[near_integer] = nearest.loc[near_integer]

            header_rate = group_key.map(effective).fillna(0.0)
            out["header_effective_tax_pct"] = header_rate
            refreshed = pd.to_numeric(
                out["item_tax1_percentage"], errors="coerce"
            ).fillna(0.0)
            use_header = (refreshed <= 0) & (header_rate > 0)
            out.loc[use_header, "item_tax1_percentage"] = header_rate.loc[use_header]
            out.loc[use_header, "tax_percentage_source"] = "SI_HEADER_EFFECTIVE_TAX"

    # Precision guard sama seperti Total SO.
    pct = pd.to_numeric(
        out["item_tax1_percentage"], errors="coerce"
    ).fillna(0.0)
    nearest = pct.round(0)
    near_integer = (pct - nearest).abs() < 0.001
    pct.loc[near_integer] = nearest.loc[near_integer]
    out["item_tax1_percentage"] = pct

    out["tax_still_unresolved"] = out["item_tax1_percentage"].le(0)
    out.drop(
        columns=["__do_key", "__product_key", "__item_name_key", "__so_doc_key", "__so_header_key"],
        inplace=True,
        errors="ignore",
    )
    return out

def _api_compat_nominal(df: pd.DataFrame) -> pd.Series:
    """Nominal line after-tax, prioritaskan transaction_total detail."""
    return _line_nominal_after_tax(df)


def _build_balance_view(stage: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    Compatibility view pengganti endpoint *-balance.

    Catatan: API lama dapat memiliki business rule server-side yang tidak terlihat
    di script ini. Karena rule endpoint tersebut tidak tersedia di source code,
    view ini hanya memproyeksikan data transaksi DB ke nama kolom yang digunakan UI.
    Tidak ada HTTP/API call lagi.
    """
    doc_labels = {
        "so": "No. SO",
        "pr": "No. PR",
        "po": "No. PO",
        "grn": "No. GRN",
        "do": "No. DO",
    }
    pic_labels = {
        "so": "PIC Sales",
        "pr": "PIC Procurement",
        "po": "PIC Procurement",
        "grn": "PIC Procurement",
        "do": "PIC Procurement",
    }

    if df is None or df.empty:
        status_col = "Status DO" if stage == "do" else "Status"
        cols = [doc_labels.get(stage, "No. Transaksi"), pic_labels.get(stage, "PIC"), status_col, "Nominal", "transaction_date"]
        return pd.DataFrame(columns=cols)

    out = pd.DataFrame(index=df.index)
    out[doc_labels.get(stage, "No. Transaksi")] = df.get("transaction_number")
    if stage == "so":
        out[pic_labels[stage]] = df.get("pic_sales_name")
    else:
        out[pic_labels.get(stage, "PIC Procurement")] = df.get("item_pic_procurement_name")

    status_name = "Status DO" if stage == "do" else "Status"
    out[status_name] = df.get("status_description").map(normalize_api_status) if "status_description" in df.columns else ""
    out["Nominal"] = _api_compat_nominal(df)
    out["transaction_date"] = pd.to_datetime(df.get("transaction_date"), errors="coerce")

    # Fallback header total hanya bila seluruh detail dokumen tidak memiliki nilai.
    # Header total diletakkan sekali saja agar tidak double count per item.
    if "transaction_total" in df.columns:
        doc_col = doc_labels.get(stage, "No. Transaksi")
        header_total = pd.to_numeric(df["transaction_total"], errors="coerce").fillna(0)
        tmp = pd.DataFrame({"doc": out[doc_col], "line": out["Nominal"], "header": header_total}, index=out.index)
        for _, idx in tmp.groupby("doc", dropna=False).groups.items():
            idx = list(idx)
            if not idx:
                continue
            line_sum = float(tmp.loc[idx, "line"].sum())
            hv = float(tmp.loc[idx, "header"].iloc[0]) if len(idx) else 0.0
            if line_sum == 0 and hv != 0:
                out.loc[idx, "Nominal"] = 0.0
                out.loc[idx[0], "Nominal"] = hv

    return out.reset_index(drop=True)




def _canonical_db_key(value):
    """Normalisasi ID database agar join detail/product stabil (1, 1.0, '1' -> '1')."""
    if value is None or pd.isna(value):
        return None
    value = str(value).strip()
    if not value or value.lower() in {"nan", "none", "null", "<na>"}:
        return None
    try:
        number = float(value)
        if number.is_integer():
            return str(int(number))
    except Exception:
        pass
    return value


def _join_unique_text(series: pd.Series) -> str:
    values = []
    for value in series.dropna():
        s = str(value).strip()
        if s and s.lower() not in {"nan", "none", "null", "<na>"} and s not in values:
            values.append(s)
    return " | ".join(values)


def _so_net_unit_price(df: pd.DataFrame) -> pd.Series:
    """
    Harga satuan SO mengikuti formula dashboard lama:
      net_unit = price - discount% + tax1%

    Bila price/formula tidak tersedia tetapi subtotal detail tersedia dan qty > 0,
    gunakan subtotal / qty sebagai fallback harga satuan efektif.
    """
    if df is None or df.empty:
        return pd.Series(dtype="float64")

    qty = pd.to_numeric(df.get("item_quantity", 0), errors="coerce").fillna(0)
    price = pd.to_numeric(df.get("item_price", 0), errors="coerce").fillna(0)
    discount = pd.to_numeric(df.get("item_discount", 0), errors="coerce").fillna(0)
    tax1 = pd.to_numeric(df.get("item_tax1_percentage", 0), errors="coerce").fillna(0)
    subtotal = pd.to_numeric(df.get("item_sub_total", 0), errors="coerce").fillna(0)

    disc_per_unit = price * (discount / 100.0)
    taxable_unit = price - disc_per_unit
    tax_per_unit = taxable_unit * (tax1 / 100.0)
    net_unit = taxable_unit + tax_per_unit

    fallback = pd.Series(0.0, index=df.index, dtype="float64")
    valid_qty = qty > 0
    fallback.loc[valid_qty] = subtotal.loc[valid_qty] / qty.loc[valid_qty]

    use_fallback = (net_unit == 0) & (fallback != 0)
    net_unit.loc[use_fallback] = fallback.loc[use_fallback]
    return net_unit.fillna(0)


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
    so["__so_qty"] = pd.to_numeric(
        so.get("item_quantity", 0), errors="coerce"
    ).fillna(0).clip(lower=0)
    so["__net_unit_price"] = _so_net_unit_price(so)
    so["__so_do_qty"] = pd.to_numeric(
        so.get("item_do_quantity", 0), errors="coerce"
    ).fillna(0).clip(lower=0)
    so["__realized_qty_diag"] = pd.to_numeric(
        so.get("item_realized_quantity", 0), errors="coerce"
    ).fillna(0).clip(lower=0)
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
        do["__direct_do_qty"] = pd.to_numeric(
            do.get("item_quantity", 0), errors="coerce"
        ).fillna(0).clip(lower=0)
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
        balance[col] = pd.to_numeric(
            balance.get(col, 0), errors="coerce"
        ).fillna(0)

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
    out["Unit Price"] = pd.to_numeric(
        balance.get("item_price", 0), errors="coerce"
    ).fillna(0)
    out["Discount %"] = pd.to_numeric(
        balance.get("item_discount", 0), errors="coerce"
    ).fillna(0)
    out["Tax1 %"] = pd.to_numeric(
        balance.get("item_tax1_percentage", 0), errors="coerce"
    ).fillna(0)
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


def _build_npr_view(data_new: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """DB-native NPR proxy: item SO yang belum memiliki PR strict detail+product."""
    so = data_new.get("so", pd.DataFrame()).copy()
    pr = data_new.get("pr", pd.DataFrame()).copy()
    if so.empty:
        return pd.DataFrame(columns=["No. Transaksi", "Status", "Nominal", "transaction_date"])

    def canon(v):
        if pd.isna(v):
            return None
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "null", "<na>"}:
            return None
        try:
            f = float(s)
            if f.is_integer():
                return str(int(f))
        except Exception:
            pass
        return s

    so["__detail"] = so.get("item_id", pd.Series(index=so.index, dtype="object")).map(canon)
    so["__product"] = so.get("item_product_id", pd.Series(index=so.index, dtype="object")).map(canon)
    if pr.empty:
        pending = so.copy()
    else:
        pr_keys = set(zip(
            pr.get("item_so_detail_id", pd.Series(index=pr.index, dtype="object")).map(canon),
            pr.get("item_product_id", pd.Series(index=pr.index, dtype="object")).map(canon),
        ))
        mask = [
            (d, p) not in pr_keys
            for d, p in zip(so["__detail"], so["__product"])
        ]
        pending = so.loc[mask].copy()

    return pd.DataFrame({
        "No. Transaksi": pending.get("transaction_number"),
        "Status": pending.get("status_description").map(normalize_api_status) if "status_description" in pending.columns else "",
        "Nominal": _api_compat_nominal(pending),
        "transaction_date": pd.to_datetime(pending.get("transaction_date"), errors="coerce"),
    }).reset_index(drop=True)


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def load_all_data_new(
    start_date=None,
    end_date=None,
    customer_lookup_version: str = CUSTOMER_LOOKUP_VERSION,
) -> dict[str, pd.DataFrame]:
    """Pengganti load API baru: semua stage dibaca langsung dari ERP PostgreSQL."""
    del customer_lookup_version  # cache-busting key
    target_end = end_date if end_date is not None else date.today()

    # SO Balance API masih memuat transaksi sejak 1-Jan-2026. Jangan membuang
    # histori tersebut hanya karena date picker saat ini lebih sempit.
    if start_date is None:
        so_start = SO_BASE_START_DATE
    else:
        requested_start = pd.Timestamp(start_date).date()
        so_start = min(requested_start, SO_BASE_START_DATE)

    result = {}
    for stage in ["so", "pr", "po", "grn", "do", "si"]:
        stage_start = so_start if stage == "so" else None
        try:
            result[stage] = read_stage_from_database(
                stage,
                start_date=stage_start,
                end_date=target_end,
            )
        except Exception as exc:
            logger.exception("Gagal membaca stage %s dari database", stage)
            st.warning(f"Gagal membaca {stage.upper()} dari database: {exc}")
            result[stage] = pd.DataFrame()

    # Revenue parity: SI sering tidak menyimpan tax percentage langsung.
    # Lengkapi dari DO/SO linkage tanpa hard-code 11%.
    result["si"] = _enrich_si_tax_from_chain(
        result.get("si", pd.DataFrame()),
        result.get("do", pd.DataFrame()),
        result.get("so", pd.DataFrame()),
    )
    return result


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def load_all_data(
    start_date=None,
    end_date=None,
    customer_lookup_version: str = CUSTOMER_LOOKUP_VERSION,
) -> dict[str, pd.DataFrame]:
    """Pengganti endpoint balance/outstanding lama; tetap 100% database."""
    del customer_lookup_version  # cache-busting key
    target_end = end_date if end_date is not None else date.today()
    source = load_all_data_new(start_date=start_date, end_date=target_end)

    result = {
        # SO Balance business rule final: hanya NO DO / PARTIAL DO.
        "so": _build_so_balance_view(
            source.get("so", pd.DataFrame()),
            source.get("do", pd.DataFrame()),
        ),
        "pr": _build_balance_view("pr", source.get("pr", pd.DataFrame())),
        "po": _build_balance_view("po", source.get("po", pd.DataFrame())),
        "grn": _build_balance_view("grn", source.get("grn", pd.DataFrame())),
        "do": _build_balance_view("do", source.get("do", pd.DataFrame())),
        "npr": _build_npr_view(source),
    }
    return result


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

    with st.spinner("Membaca data langsung dari PostgreSQL ERP..."):
        data_new = load_all_data_new(start_date=start_date, end_date=end_date)
        data_old = load_all_data(start_date=None, end_date=end_date)
        # Cached call: source historis yang sama dengan pembentuk SO Balance.
        debug_balance_source = load_all_data_new(start_date=None, end_date=end_date)

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
        "item_product_code": "product_code",
        "item_item_name" : "item_name",
        "customer_name": "Customer",
        "customer_id": "Customer ID",
    })
    # Second-pass customer lookup agar data cache lama yang masih berupa ID
    # ikut dikonversi menjadi nama customer.
    df_so_final = apply_customer_name_lookup(
        df_so_final,
        id_col="Customer ID",
        name_col="Customer",
    )

    #PR
    df_pr_final = df_pr_final.rename(columns={
        "item_pic_procurement_name": "PIC Procurement",
        "status_description": "Status_pr",
        "item_id": "pr_detail_id",
        "item_so_detail_id" : "so_detail_id",
        "transaction_number" : "transaction_number_pr",
        "item_product_id" : "product_id",
        "item_product_code": "product_code"
    })
    #PO
    df_po_final = df_po_final.rename(columns={
        "item_pic_procurement_name": "PIC Procurement",
        "status_description": "Status_po",
        "item_id": "po_detail_id",
        "item_pr_detail_id" : "pr_detail_id",
        "transaction_number" : "transaction_number_po",
        "item_product_id" : "product_id",
        "item_product_code": "product_code"
    })
    #GRN
    df_grn_final = df_grn_final.rename(columns={
        "item_pic_procurement_name": "PIC Procurement",
        "status_description": "Status_grn",
        "item_id": "grn_detail_id",
        "item_po_detail_id" : "po_detail_id",
        "transaction_number" : "transaction_number_grn",
        "item_product_id" : "product_id",
        "item_product_code": "product_code"
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
        "item_product_code": "product_code",
        "customer_name": "Customer",
        "customer_id": "Customer ID",
        "item_item_name": "item_name"
})

    # Second-pass lama tetap dijalankan terlebih dahulu.
    df_si_final = apply_customer_name_lookup(
        df_si_final,
        id_col="Customer ID",
        name_col="Customer",
    )

    # =========================================================
    # CUSTOMER ACTUAL-ID PROBE
    # =========================================================
    # Jika hasil masih berupa ID seperti 3238 / 1560 / 997,
    # probe akan mencari ID aktual tersebut ke tabel-tabel public
    # dan memilih source yang mempunyai coverage ID terbesar.
    df_si_final, customer_probe_diagnostic = apply_customer_name_probe(
        df_si_final,
        id_col="Customer ID",
        name_col="Customer",
    )

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
        # Gunakan start_date dan end_date dari Select Date Range.
        # Contoh: jika user memilih 1 Sep 2026 s/d 23 Sep 2026,
        # maka Total SO hanya menghitung SO dengan transaction_date
        # di dalam periode tersebut.
        df_so_final_real = apply_realization_filter(
            df_so_final,
            report_start_date,
            report_end_date
        )

        debug_so_period_filtered = df_so_final_real.copy()

        # Dataset lain (PR, PO, GRN, DO, SI) ambil SEMUA data tanpa batasan start_date
        df_pr_final_real = apply_cumulative_filter(df_pr_final, report_end_date)
        df_po_final_real = apply_cumulative_filter(df_po_final, report_end_date)
        df_grn_final_real = apply_cumulative_filter(df_grn_final, report_end_date)
        df_do_final_real = apply_cumulative_filter(df_do_final, report_end_date)
        df_si_final_real = apply_cumulative_filter(df_si_final, report_end_date)
        # Revenue/Pareto harus mengikuti periode yang dipilih, seperti API.
        # Dataset cumulative tetap dipertahankan terpisah untuk kebutuhan chain SO→DO→SI.
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
    df_so_final_real["nominal_so"] = _line_nominal_after_tax(df_so_final_real)

    df_so_final_real["Status_so"] = (
        df_so_final_real["Status_so"]
        .map(normalize_api_status)
        .fillna("")
        .astype(str)
        .str.strip()
    )

    status_filter = ['In Progress', 'Approved', 'Complete']
    df_so_status_valid = df_so_final_real[
        df_so_final_real['Status_so'].isin(status_filter)
    ].copy()
    debug_so_status_valid = df_so_status_valid.copy()

    keyword_to_exclude = ['Jasa', 'Biaya', 'Admin', 'Pengiriman']
    pattern = '|'.join([re.escape(word) for word in keyword_to_exclude])

    df_so_total = df_so_status_valid.copy()
    if 'item_name' in df_so_total.columns:
        df_so_total = df_so_total[
            ~df_so_total['item_name'].astype(str).str.contains(
                pattern, case=False, na=False
            )
        ].copy()

    df_so_total["disc_per_unit"] = df_so_total["item_price"] * (df_so_total["item_discount"] / 100)
    df_so_total["tax_unit"] = (df_so_total["item_price"] - df_so_total["disc_per_unit"]) * (df_so_total["item_tax1_percentage"] / 100)
    df_so_total["net_price_unit"] = df_so_total["item_price"] - df_so_total["disc_per_unit"] + df_so_total["tax_unit"]
    df_so_total["total_so_row"] = _line_nominal_after_tax(df_so_total)

    # =========================================================
    # FINAL TOTAL SO
    # =========================================================
    # Historical/revision SO detail sudah dibuang pada reader PostgreSQL
    # menggunakan current/active/soft-delete predicate (terutama deleted_at IS NULL).
    # Karena itu Total SO harus kembali menggunakan SUM detail current agar 1:1
    # dengan business logic API, bukan header transaction_total.
    total_so = float(
        pd.to_numeric(
            df_so_total["total_so_row"], errors="coerce"
        ).fillna(0).sum()
    )

    # Header hanya dipertahankan sebagai CONTROL / AUDIT, bukan sumber card.
    _, total_so_header_gross, total_so_excluded_keyword = (
        _reconciled_total_so_from_header(df_so_status_valid, df_so_total)
    )

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
            source_label="PostgreSQL ERP",
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
    df_si_total["total_si_row"] = _line_nominal_after_tax(df_si_total)
    total_si = df_si_total["total_si_row"].sum()

    debug_revenue_final = df_si_total.copy()
    revenue_debug_snapshots = {
        "RAW_SOURCE": debug_revenue_raw_source,
        "PERIOD_FILTERED": debug_revenue_period_filtered,
        "STATUS_VALID": debug_revenue_status_valid,
        "FINAL_REVENUE": debug_revenue_final,
    }
    debug_revenue_summary, debug_revenue_excel, debug_revenue_gap = build_revenue_debug_package(
        "PostgreSQL ERP", revenue_debug_snapshots, float(total_si)
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
    
    # Card jumlah transaksi/item harus memakai populasi yang sama dengan Card Total SO.
    total_so_count = safe_unique_count(df_so_total, "transaction_number_so")
    total_so_balance_count = safe_unique_count(df_so_f, "No. SO")
    total_so_rows = len(df_so_total)
    total_so_balance_rows = len(df_so_f)

    total_pr_count = safe_unique_count(df_pr_final_real, "transaction_number_pr")
    total_pr_balance_count = safe_unique_count(df_pr_f, "No. PR")
    total_pr_rows = len(df_pr_final_real)
    total_pr_balance_rows = len(df_pr_f)
    total_do_count = safe_unique_count(df_do_final_real, "transaction_number_do")
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
        build_so_balance_debug_package("PostgreSQL ERP", df_so, df_so_f)
    )

    # Phase 4/5 PR-membership forensic tidak lagi dibutuhkan karena SO Balance sekarang DO-only.
    pic_candidate_cols, pic_foreign_keys, pic_header_sample = probe_sales_pic_schema()
    debug_pic_excel = build_pic_debug_excel(
        df_so_final_real, pic_candidate_cols, pic_foreign_keys, pic_header_sample
    )

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


    # -----------------------------------------------------
    # SO BELUM DO - gunakan dataset SO Balance yang sudah memakai
    # SO detail do_quantity + direct DO + SI fallback.
    # Jangan lagi menentukan "belum DO" hanya dari hasil merge direct.
    # -----------------------------------------------------
    if not df_so_f.empty and "Balance Type" in df_so_f.columns:
        df_so_belum_do = df_so_f[df_so_f["Balance Type"] == "NO DO"].copy()
        total_item_belum_do = (
            int(df_so_belum_do["SO Detail ID"].nunique())
            if "SO Detail ID" in df_so_belum_do.columns else len(df_so_belum_do)
        )
        total_dokumen_belum_do = (
            int(df_so_belum_do["No. SO"].nunique())
            if "No. SO" in df_so_belum_do.columns else 0
        )
        total_nominal_so_belum_do = float(
            pd.to_numeric(df_so_belum_do.get("Nominal", 0), errors="coerce").fillna(0).sum()
        )
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

            # =====================================================
            # DOWNLOAD SO BALANCE - DITEMPATKAN SETELAH SELURUH GRAFIK
            # =====================================================
            with st.container(border=True):
                st.subheader("📥 Download Data SO Balance")

                if not df_so_f.empty and "Status" in df_so_f.columns:
                    all_statuses = sorted(
                        [
                            s
                            for s in df_so_f["Status"]
                            .dropna()
                            .astype(str)
                            .unique()
                            .tolist()
                            if s.strip()
                        ]
                    )

                    selected_statuses = st.multiselect(
                        "Pilih Status SO Balance untuk di-download:",
                        options=all_statuses,
                        default=all_statuses,
                        key="so_balance_status_export",
                    )

                    df_download_so_balance = df_so_f[
                        df_so_f["Status"].isin(selected_statuses)
                    ].copy()

                    total_download_balance = float(
                        pd.to_numeric(
                            df_download_so_balance.get("Nominal", 0),
                            errors="coerce",
                        )
                        .fillna(0)
                        .sum()
                    )

                    total_download_docs = (
                        int(df_download_so_balance["No. SO"].nunique())
                        if "No. SO" in df_download_so_balance.columns
                        else 0
                    )

                    st.caption(
                        "SO Balance = sisa Qty SO yang belum di-DO-kan. Hanya NO DO + PARTIAL DO. "
                        "Balance Qty = SO Qty - Effective DO Qty. PR/SI tidak mengurangi balance; "
                        "SO berstatus Draft, Need Approve, dan Complete dikeluarkan."
                    )

                    d1, d2, d3 = st.columns(3)
                    with d1:
                        metric_card(
                            "Nominal Download",
                            f"Rp {total_download_balance:,.0f}".replace(",", "."),
                        )
                    with d2:
                        metric_card(
                            "Dokumen SO",
                            f"{total_download_docs:,}",
                        )
                    with d3:
                        metric_card(
                            "Item Balance",
                            f"{len(df_download_so_balance):,}",
                        )

                    if not df_download_so_balance.empty:
                        st.download_button(
                            label=(
                                f"⬇️ Download SO Balance "
                                f"({len(df_download_so_balance):,} Baris).xlsx"
                            ),
                            data=to_excel_bytes(
                                df_download_so_balance,
                                sheet_name="SO_Balance",
                            ),
                            file_name=(
                                "SO_Balance_"
                                + datetime.now().strftime("%Y%m%d_%H%M%S")
                                + ".xlsx"
                            ),
                            mime=(
                                "application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet"
                            ),
                            key="download_so_balance_after_chart",
                            use_container_width=True,
                        )
                    else:
                        st.warning(
                            "Tidak ada data SO Balance yang sesuai dengan "
                            "status yang dipilih."
                        )
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
                        legend=dict(
                            orientation="h",
                            yanchor="bottom",
                            y=1.08,
                            xanchor="center",
                            x=0.5,
                        ),
                        margin=dict(t=100, b=180, l=80, r=80),
                    )

                    st.plotly_chart(fig_concentration, use_container_width=True)


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
                        metric_card("Retention Terakhir", f"{latest_retention:.1%}")
                    with c2:
                        metric_card("Customer Aktif", f"{latest_row['Active_Customers']:,.0f}")
                    with c3:
                        metric_card("Customer Baru", f"{latest_row['New_Customers']:,.0f}")
                    with c4:
                        metric_card("Customer Kembali", f"{latest_row['Returning_Customers']:,.0f}")

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
                    st.plotly_chart(fig_retention, use_container_width=True)

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

        customer_master_cfg = discover_customer_master_table(
            "public.x4_sales_invoices"
        )

        if customer_master_cfg:
            customer_master_source = customer_master_cfg["table"]
            customer_master_id_col = customer_master_cfg["id_col"]
            customer_master_name_col = customer_master_cfg["name_col"]
            customer_discovery_method = customer_master_cfg.get(
                "discovery_method",
                "-"
            )
        else:
            customer_master_source = "Tidak ditemukan"
            customer_master_id_col = "-"
            customer_master_name_col = "-"
            customer_discovery_method = "-"

        customer_diag = customer_lookup_diagnostic(
            df_si_final,
            id_col="Customer ID",
            name_col="Customer",
        )

        st.markdown(
            f"""
- **Data Source:** `PostgreSQL ERP`
- **Connection:** `SSH Tunnel → SQLAlchemy → PostgreSQL`
- **Tanggal report sampai:** `{selected_report_date}`
- **Mode filter tanggal:** Total SO/Revenue mengikuti periode; SO Balance DO-only bersifat outstanding historis sampai tanggal akhir dan tersedia sejak `{SO_BASE_START_DATE}`
- **Cache Database Query:** `{DB_CACHE_TTL}` detik
- **Customer Master:** `{customer_master_source}`
- **Customer Master ID Column:** `{customer_master_id_col}`
- **Customer Master Name Column:** `{customer_master_name_col}`
- **Customer Discovery Method:** `{customer_discovery_method}`
- **SI Customer Rows:** `{customer_diag["total_rows"]}`
- **Customer Name Resolved:** `{customer_diag["resolved_name"]}`
- **Customer Still ID/Blank:** `{customer_diag["still_id"]}`
- **Customer Lookup Version:** `{CUSTOMER_LOOKUP_VERSION}`
- **Customer Probe Source:** `{customer_probe_diagnostic.get("best_source", {}).get("table", "-") if customer_probe_diagnostic.get("best_source") else "-"}`
- **Customer Probe ID Column:** `{customer_probe_diagnostic.get("best_source", {}).get("id_col", "-") if customer_probe_diagnostic.get("best_source") else "-"}`
- **Customer Probe Name Column:** `{customer_probe_diagnostic.get("best_source", {}).get("name_col", "-") if customer_probe_diagnostic.get("best_source") else "-"}`
- **Customer Probe Resolved IDs:** `{len(customer_probe_diagnostic.get("resolved_ids", []))}`
- **Customer Probe Unresolved IDs:** `{len(customer_probe_diagnostic.get("unresolved_ids", []))}`
- **API/HTTP Request:** `DISABLED / TIDAK DIGUNAKAN`
            """
        )

        # Tampilkan contoh mapping agar mudah memastikan customer sudah benar.
        customer_master_preview = load_customer_master()
        if not customer_master_preview.empty:
            st.caption("Contoh mapping Customer ID → Customer Name")
            st.dataframe(
                customer_master_preview[
                    ["customer_id", "customer_name"]
                ].head(20),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.warning(
                "Master customer belum berhasil ditemukan. "
                "Jika Customer masih berupa angka, cek relasi FK customer_id "
                "atau nama tabel master customer di database."
            )


if __name__ == "__main__":
    main()
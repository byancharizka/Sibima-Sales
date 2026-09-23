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

# Batas historis SO yang memang dipakai oleh logic dashboard lama.
# Reader DB memastikan SO sejak tanggal ini tetap tersedia walaupun user
# memilih date range yang lebih sempit, karena filtering final dilakukan
# kembali di main().
SO_BASE_START_DATE = date(2026, 1, 11)


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

# SO Balance hanya mengecualikan status yang belum sah menjadi outstanding order.
# Sesuai business rule terbaru: Draft dan Need Approve TIDAK masuk SO Balance.
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
        ["pic_sales_name", "sales_pic_name", "sales_name", "sales_person_name", "sales_person", "pic_sales", "sales_id", "pic_sales_id"],
    )
    customer = _first_existing(
        header_cols,
        ["customer_name", "customer", "client_name", "partner_name", "customer_id"],
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
    item_tax1_pct = _first_existing(detail_cols, ["tax1_percentage", "tax_1_percentage", "tax_percentage", "vat_percentage"])
    item_tax2_pct = _first_existing(detail_cols, ["tax2_percentage", "tax_2_percentage"])
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
        ["pic_sales_name", "sales_pic_name", "sales_name", "sales_person_name", "sales_person", "pic_sales", "sales_id", "pic_sales_id"],
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

    # Header/item aliases sengaja meniru payload API lama agar seluruh logic dashboard
    # setelah bagian LOAD DATA tidak perlu ditulis ulang besar-besaran.
    select_parts = [
        _select_expr("h", header_id, "header_id"),
        _select_expr("h", tx_number, "transaction_number", "text"),
        _select_expr("h", header_date, "transaction_date", "timestamp"),
        _select_expr("h", status, "status_description", "text"),
        _coalesce_text_expr("d", detail_sales_pic, "h", sales_pic, "pic_sales_name"),
        _select_expr("h", customer, "customer_name", "text"),
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
        _select_expr("d", item_tax2_pct, "item_tax2_percentage", "numeric"),
        _select_expr("d", item_subtotal, "item_sub_total", "numeric"),
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

    # Simpan raw status untuk diagnostic, lalu bentuk `status_description` seperti
    # payload API lama. Ini memperbaiki filter Total SO, Revenue, Pareto, pie chart, dll.
    if "status_description" in df.columns:
        df["status_raw_db"] = df["status_description"]
        df["status_description"] = df["status_description"].map(normalize_api_status)

    df = safe_to_datetime(df, "transaction_date")
    for col in ["date_approved", "date_inprogress", "date_complete"]:
        df = safe_to_datetime(df, col)
    return df


def _api_compat_nominal(df: pd.DataFrame) -> pd.Series:
    """Hitung nominal baris dari field detail DB dengan fallback subtotal."""
    if df.empty:
        return pd.Series(dtype="float64")

    qty = pd.to_numeric(df.get("item_quantity", 0), errors="coerce").fillna(0)
    price = pd.to_numeric(df.get("item_price", 0), errors="coerce").fillna(0)
    discount = pd.to_numeric(df.get("item_discount", 0), errors="coerce").fillna(0)
    tax1 = pd.to_numeric(df.get("item_tax1_percentage", 0), errors="coerce").fillna(0)
    direct = pd.to_numeric(df.get("item_sub_total", 0), errors="coerce").fillna(0)

    disc_per_unit = price * (discount / 100)
    tax_per_unit = (price - disc_per_unit) * (tax1 / 100)
    computed = qty * (price - disc_per_unit + tax_per_unit)

    value = direct.copy()
    use_computed = (computed != 0) & ((value == 0) | value.isna())
    value.loc[use_computed] = computed.loc[use_computed]
    return value.fillna(0)


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


def _build_so_balance_view(so_df: pd.DataFrame, do_df: pd.DataFrame) -> pd.DataFrame:
    """
    SO BALANCE = nilai sisa quantity SO yang belum terealisasi menjadi DO.

    Business rule status:
      - Draft        -> EXCLUDED
      - Need Approve -> EXCLUDED
      - Status lain  -> tetap dapat masuk selama Balance Qty > 0

    Grain:
      1 row per SO detail + product.

    Sumber quantity delivery dibuat berlapis agar SO yang sebenarnya sudah DO/SI
    tidak salah masuk kembali ke SO Balance:

      1) x4_sales_order_detail.do_quantity        -> primary operational source
      2) SUM x4_delivery_order_details.quantity   -> direct SO-detail cross-check
      3) x4_sales_order_detail.si_quantity        -> safety fallback; SI pada flow ERP
                                                    membuktikan item sudah melewati DO

    Effective Delivered Qty = nilai TERBESAR dari tiga source di atas, minimum 0,
    kemudian dibatasi maksimum SO Qty untuk perhitungan balance.

    Balance Qty = MAX(SO Qty - Effective Delivered Qty, 0)
    SO Balance  = Balance Qty * Net Unit Price SO

    Catatan:
    - `realized_quantity` hanya ditampilkan sebagai diagnostic dan TIDAK dipakai
      otomatis sebagai delivered qty karena semantik bisnisnya belum dikunci.
    - DO direct tetap memakai strict key SO detail_id + product_id.
    - SI quantity hanya safety fallback. Bila SI > DO source, row diberi diagnostic
      agar mudah diaudit.
    """
    output_cols = [
        "No. SO", "PIC Sales", "Status", "Nominal", "transaction_date",
        "SO Detail ID", "Product ID", "Item Name",
        "SO Qty", "SO Detail DO Qty", "DO Qty (Direct)", "SO Detail SI Qty",
        "Effective Delivered Qty", "DO Qty", "Balance Qty",
        "Unit Price", "Discount %", "Tax1 %", "Net Unit Price",
        "SO Nominal", "Delivered Nominal Proxy",
        "Balance Type", "Quantity Source", "Diagnostic",
        "SO Detail DO Status", "SO Detail SI Status", "Realized Qty (Diagnostic)",
        "DO Documents", "DO Detail Count",
    ]

    if so_df is None or so_df.empty:
        return pd.DataFrame(columns=output_cols)

    so = so_df.copy()
    do = do_df.copy() if do_df is not None else pd.DataFrame()

    # ---------------------------------------------------------
    # SO: satu row per detail/item
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

    # Quantity progress langsung dari SO detail.
    so["__so_do_qty"] = pd.to_numeric(
        so.get("item_do_quantity", 0), errors="coerce"
    ).fillna(0).clip(lower=0)
    so["__so_si_qty"] = pd.to_numeric(
        so.get("item_si_quantity", 0), errors="coerce"
    ).fillna(0).clip(lower=0)
    so["__realized_qty_diag"] = pd.to_numeric(
        so.get("item_realized_quantity", 0), errors="coerce"
    ).fillna(0).clip(lower=0)

    # PK detail yang sama tidak boleh dihitung dua kali.
    valid_key = so["__so_detail_key"].notna()
    so_valid = so.loc[valid_key].drop_duplicates(
        subset=["__so_detail_key", "__product_key"], keep="first"
    )
    so_invalid = so.loc[~valid_key].copy()
    so = pd.concat([so_valid, so_invalid], ignore_index=True, sort=False)

    # ---------------------------------------------------------
    # DO direct: aggregate seluruh delivery dengan strict
    # SO.detail_id + product_id.
    # ---------------------------------------------------------
    if do.empty:
        do_summary = pd.DataFrame(
            columns=[
                "__so_detail_key", "__product_key", "__direct_do_qty",
                "__do_docs", "__do_detail_count",
            ]
        )
    else:
        do["__so_detail_key"] = do.get(
            "item_so_detail_id", pd.Series(index=do.index, dtype="object")
        ).map(_canonical_db_key)
        do["__product_key"] = do.get(
            "item_product_id", pd.Series(index=do.index, dtype="object")
        ).map(_canonical_db_key)
        do["__direct_do_qty"] = pd.to_numeric(
            do.get("item_quantity", 0), errors="coerce"
        ).fillna(0).clip(lower=0)

        do_linked = do[
            do["__so_detail_key"].notna()
            & do["__product_key"].notna()
        ].copy()

        if do_linked.empty:
            do_summary = pd.DataFrame(
                columns=[
                    "__so_detail_key", "__product_key", "__direct_do_qty",
                    "__do_docs", "__do_detail_count",
                ]
            )
        else:
            # Hindari double count bila detail DO yang sama muncul lebih dari sekali.
            if "item_id" in do_linked.columns:
                do_linked["__do_detail_key"] = do_linked["item_id"].map(_canonical_db_key)
                do_linked = do_linked.drop_duplicates(
                    subset=["__do_detail_key", "__so_detail_key", "__product_key"],
                    keep="first",
                )

            agg_dict = {
                "__direct_do_qty": "sum",
                "transaction_number": _join_unique_text,
            }
            if "item_id" in do_linked.columns:
                agg_dict["item_id"] = lambda x: x.dropna().astype(str).nunique()

            do_summary = (
                do_linked.groupby(
                    ["__so_detail_key", "__product_key"], dropna=False
                )
                .agg(agg_dict)
                .reset_index()
                .rename(columns={
                    "transaction_number": "__do_docs",
                    "item_id": "__do_detail_count",
                })
            )
            if "__do_detail_count" not in do_summary.columns:
                do_summary["__do_detail_count"] = 0

    balance = so.merge(
        do_summary,
        how="left",
        on=["__so_detail_key", "__product_key"],
    )

    balance["__direct_do_qty"] = pd.to_numeric(
        balance.get("__direct_do_qty", 0), errors="coerce"
    ).fillna(0).clip(lower=0)
    balance["__do_docs"] = balance.get(
        "__do_docs", pd.Series("", index=balance.index, dtype="object")
    ).fillna("")
    balance["__do_detail_count"] = pd.to_numeric(
        balance.get("__do_detail_count", 0), errors="coerce"
    ).fillna(0).astype(int)

    # ---------------------------------------------------------
    # Effective Delivered Qty
    # ---------------------------------------------------------
    qty_sources = balance[["__so_do_qty", "__direct_do_qty", "__so_si_qty"]].copy()
    balance["__effective_delivered_raw"] = qty_sources.max(axis=1).fillna(0).clip(lower=0)

    # Untuk balance monetary, delivery tidak boleh melebihi SO Qty.
    balance["__effective_delivered_qty"] = balance[
        ["__effective_delivered_raw", "__so_qty"]
    ].min(axis=1)

    balance["__balance_qty"] = (
        balance["__so_qty"] - balance["__effective_delivered_qty"]
    ).clip(lower=0)

    # Source label: tunjukkan source yang menentukan effective qty.
    def quantity_source(row):
        effective_raw = float(row.get("__effective_delivered_raw") or 0)
        if effective_raw <= 0:
            return "NO DELIVERY QTY"

        sources = []
        eps = 1e-9
        if abs(float(row.get("__so_do_qty") or 0) - effective_raw) <= eps:
            sources.append("SO_DETAIL_DO_QTY")
        if abs(float(row.get("__direct_do_qty") or 0) - effective_raw) <= eps:
            sources.append("DO_DETAIL_DIRECT")
        if abs(float(row.get("__so_si_qty") or 0) - effective_raw) <= eps:
            sources.append("SO_DETAIL_SI_QTY_FALLBACK")
        return " | ".join(sources) if sources else "UNKNOWN"

    balance["__qty_source"] = balance.apply(quantity_source, axis=1)

    # Diagnostic detail agar kasus salah dapat ditelusuri dari hasil download.
    def diagnostic(row):
        notes = []
        so_qty = float(row.get("__so_qty") or 0)
        so_do = float(row.get("__so_do_qty") or 0)
        direct_do = float(row.get("__direct_do_qty") or 0)
        si_qty = float(row.get("__so_si_qty") or 0)
        raw_eff = float(row.get("__effective_delivered_raw") or 0)

        if abs(so_do - direct_do) > 1e-9 and (so_do > 0 or direct_do > 0):
            notes.append("DO QTY MISMATCH")
        if direct_do == 0 and so_do > 0:
            notes.append("DIRECT DO LINK MISSING; USING SO DO QTY")
        if si_qty > max(so_do, direct_do) + 1e-9:
            notes.append("SI QTY > DO SOURCE; SI FALLBACK USED")
        if si_qty > 0 and so_do == 0 and direct_do == 0:
            notes.append("SI EXISTS BUT DO QTY NOT FOUND")
        if raw_eff > so_qty + 1e-9:
            notes.append("DELIVERED QTY > SO QTY; CAPPED FOR BALANCE")
        if so_qty <= 0:
            notes.append("SO QTY <= 0")
        if not notes:
            notes.append("OK")
        return " | ".join(notes)

    balance["__diagnostic"] = balance.apply(diagnostic, axis=1)

    # ---------------------------------------------------------
    # STATUS FILTER SO BALANCE
    # Draft dan Need Approve tidak boleh masuk SO Balance, pie chart,
    # jumlah transaksi/item balance, maupun file download SO Balance.
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

    # ---------------------------------------------------------
    # Hanya outstanding/partial yang masuk SO Balance.
    # Fully delivered / fully invoiced akan hilang dari dataset balance.
    # ---------------------------------------------------------
    balance = balance[balance["__balance_qty"] > 0].copy()

    if balance.empty:
        return pd.DataFrame(columns=output_cols)

    balance["__balance_nominal"] = (
        balance["__balance_qty"] * balance["__net_unit_price"]
    )
    balance["__so_nominal"] = (
        balance["__so_qty"] * balance["__net_unit_price"]
    )
    balance["__delivered_nominal_proxy"] = (
        balance["__effective_delivered_qty"] * balance["__net_unit_price"]
    )

    balance["__balance_type"] = "NO DO"
    partial_mask = (
        (balance["__effective_delivered_qty"] > 0)
        & (balance["__effective_delivered_qty"] < balance["__so_qty"])
    )
    balance.loc[partial_mask, "__balance_type"] = "PARTIAL DO"

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
    out["DO Qty (Direct)"] = balance["__direct_do_qty"]
    out["SO Detail SI Qty"] = balance["__so_si_qty"]
    out["Effective Delivered Qty"] = balance["__effective_delivered_qty"]
    # Compatibility/audit column: DO Qty berarti qty efektif yang dipakai balance.
    out["DO Qty"] = balance["__effective_delivered_qty"]
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
    out["SO Detail SI Status"] = balance.get("item_si_status")
    out["Realized Qty (Diagnostic)"] = balance["__realized_qty_diag"]
    out["DO Documents"] = balance["__do_docs"]
    out["DO Detail Count"] = balance["__do_detail_count"]

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
def load_all_data_new(start_date=None, end_date=None) -> dict[str, pd.DataFrame]:
    """Pengganti load API baru: semua stage dibaca langsung dari ERP PostgreSQL."""
    target_end = end_date if end_date is not None else date.today()

    # Dashboard lama kemudian memfilter SO mulai 11-Jan-2026. Jangan membuang data
    # tersebut hanya karena date picker saat ini lebih sempit.
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
    return result


@st.cache_data(ttl=DB_CACHE_TTL, show_spinner=False)
def load_all_data(start_date=None, end_date=None) -> dict[str, pd.DataFrame]:
    """Pengganti endpoint balance/outstanding lama; tetap 100% database."""
    target_end = end_date if end_date is not None else date.today()
    source = load_all_data_new(start_date=start_date, end_date=target_end)

    result = {
        # SO Balance menggunakan sisa qty SO setelah dikurangi seluruh qty DO
        # pada strict key SO detail_id + product_id.
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
        .map(normalize_api_status)
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
                        "Data yang di-download adalah hasil perhitungan SO Balance "
                        "(NO DO + PARTIAL DO), setelah Draft, Need Approve, dan Complete dikeluarkan."
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

        st.markdown(
            f"""
- **Data Source:** `PostgreSQL ERP`
- **Connection:** `SSH Tunnel → SQLAlchemy → PostgreSQL`
- **Tanggal report sampai:** `{selected_report_date}`
- **Mode filter tanggal:** kumulatif untuk downstream; SO tersedia sejak `{SO_BASE_START_DATE}`
- **Cache Database Query:** `{DB_CACHE_TTL}` detik
- **API/HTTP Request:** `DISABLED / TIDAK DIGUNAKAN`
            """
        )


if __name__ == "__main__":
    main()
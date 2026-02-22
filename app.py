import streamlit as st
from supabase import create_client, Client
import pandas as pd
from datetime import datetime, date, timedelta
import time
import io
from fpdf import FPDF

# ---------- Page Config (must be first) ----------
st.set_page_config(page_title="Medical Store", layout="wide", initial_sidebar_state="auto")

# ---------- Custom CSS for Mobile Responsiveness ----------
st.markdown("""
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
    /* Make tables scrollable on small screens */
    .stDataFrame { overflow-x: auto; }
    /* Adjust sidebar width on mobile */
    @media (max-width: 768px) {
        .css-1d391kg { width: 100%; }
        .stButton button { width: 100%; }
    }
</style>
""", unsafe_allow_html=True)

# ---------- Supabase Initialization ----------
@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_supabase()

# ---------- Offline Queue Management ----------
def init_queue():
    if "pending_ops" not in st.session_state:
        st.session_state.pending_ops = []  # list of (table, data, method)

def add_pending_op(table, data, method="insert"):
    st.session_state.pending_ops.append((table, data, method))

def flush_queue():
    if not st.session_state.pending_ops:
        return True
    success = True
    remaining = []
    for table, data, method in st.session_state.pending_ops:
        try:
            if method == "insert":
                supabase.table(table).insert(data).execute()
            elif method == "update":
                supabase.table(table).update(data).eq("id", data["id"]).execute()
            elif method == "delete":
                supabase.table(table).delete().eq("id", data["id"]).execute()
            elif method == "upsert":
                supabase.table(table).upsert(data).execute()
        except Exception as e:
            st.warning(f"Offline: operation pending ({table})")
            remaining.append((table, data, method))
            success = False
    st.session_state.pending_ops = remaining
    return success

# ---------- Helper Functions ----------
def login(username, password):
    try:
        response = supabase.table("users").select("*").eq("username", username).eq("password", password).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        st.error("Login service unavailable. Please check your connection.")
        return None

def get_settings():
    try:
        response = supabase.table("settings").select("*").execute()
        return {item["key"]: item["value"] for item in response.data}
    except:
        return {}

def update_setting(key, value):
    try:
        supabase.table("settings").upsert({"key": key, "value": value}).execute()
    except:
        add_pending_op("settings", {"key": key, "value": value}, "upsert")

def get_active_expense_heads():
    try:
        response = supabase.table("expense_heads").select("*").eq("is_active", True).execute()
        return response.data
    except:
        return []

def get_active_vendors():
    try:
        response = supabase.table("vendors").select("*").eq("is_active", True).execute()
        return response.data
    except:
        return []

def get_today_shift(date_obj, shift_name):
    """Get open shift for given date and shift, or create if not exists."""
    try:
        response = supabase.table("shifts").select("*").eq("date", date_obj.isoformat()).eq("shift", shift_name).eq("status", "open").execute()
        if response.data:
            return response.data[0]
        else:
            # Determine opening cash: previous shift's actual closing or 0
            prev_shift = get_previous_shift(date_obj, shift_name)
            opening = prev_shift["actual_closing"] if prev_shift else 0.0
            data = {"date": date_obj.isoformat(), "shift": shift_name, "opening_cash": opening, "status": "open"}
            try:
                resp = supabase.table("shifts").insert(data).execute()
                return resp.data[0]
            except:
                add_pending_op("shifts", data)
                return {"id": None, "date": date_obj, "shift": shift_name, "opening_cash": opening, "status": "open"}
    except Exception as e:
        st.error(f"Error accessing shift: {e}")
        return None

def get_previous_shift(current_date, current_shift):
    shift_order = {"Morning": 1, "Evening": 2, "Night": 3}
    cur_order = shift_order[current_shift]
    try:
        if cur_order > 1:
            prev_shift_name = [k for k, v in shift_order.items() if v == cur_order - 1][0]
            resp = supabase.table("shifts").select("*").eq("date", current_date.isoformat()).eq("shift", prev_shift_name).eq("status", "closed").execute()
            if resp.data:
                return resp.data[0]
        prev_date = current_date - timedelta(days=1)
        resp = supabase.table("shifts").select("*").eq("date", prev_date.isoformat()).eq("shift", "Night").eq("status", "closed").execute()
        if resp.data:
            return resp.data[0]
        return None
    except:
        return None

def get_shift_transactions(shift_id):
    if not shift_id:
        return []
    try:
        resp = supabase.table("transactions").select("*").eq("shift_id", shift_id).execute()
        return resp.data
    except:
        return []

def compute_expected_cash(shift):
    transactions = get_shift_transactions(shift["id"])
    cash = shift["opening_cash"]
    for t in transactions:
        if t["type"] == "sale":
            cash += t["amount"]
        elif t["type"] == "return":
            cash -= t["amount"]
        elif t["type"] in ["expense", "vendor_payment", "purchase", "withdrawal"]:
            if t.get("source") == "sales":
                cash -= t["amount"]
    return cash

def close_shift(shift_id, actual_cash):
    try:
        resp = supabase.table("shifts").select("*").eq("id", shift_id).execute()
        if not resp.data:
            return False
        shift = resp.data[0]
        expected = compute_expected_cash(shift)
        shortage = actual_cash - expected
        supabase.table("shifts").update({
            "expected_closing": expected,
            "actual_closing": actual_cash,
            "shortage": shortage,
            "status": "closed"
        }).eq("id", shift_id).execute()
        return True
    except Exception as e:
        st.error(f"Error closing shift: {e}")
        return False

def get_shop_details():
    settings = get_settings()
    return {
        "name": settings.get("shop_name", "Medical Store"),
        "address": settings.get("shop_address", "")
    }

def generate_pdf(title, date_range_str, columns, data, shop_details):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, shop_details["name"], ln=1, align="C")
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 6, shop_details["address"], ln=1, align="C")
    pdf.cell(0, 6, title, ln=1, align="C")
    pdf.cell(0, 6, f"Date Range: {date_range_str}", ln=1, align="C")
    pdf.ln(5)
    pdf.set_font("Arial", "B", 10)
    col_width = pdf.w / (len(columns) + 1) if len(columns) < 6 else pdf.w / (len(columns) + 0.5)
    for col in columns:
        pdf.cell(col_width, 8, col, border=1)
    pdf.ln()
    pdf.set_font("Arial", "", 9)
    for row in data:
        for item in row:
            pdf.cell(col_width, 6, str(item), border=1)
        pdf.ln()
    return pdf.output(dest='S').encode('latin1')

# ---------- Session State ----------
init_queue()
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
    st.session_state.user = None
    st.session_state.role = None
    st.session_state.page = "Dashboard"

flush_queue()

# ---------- Custom App Styling ----------
settings = get_settings()
app_css = settings.get("app_css", "")
if app_css:
    st.markdown(f"<style>{app_css}</style>", unsafe_allow_html=True)

# ---------- Login Page ----------
if not st.session_state.authenticated:
    st.title("🔐 Medical Store Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
        if submitted:
            user = login(username, password)
            if user:
                st.session_state.authenticated = True
                st.session_state.user = user
                st.session_state.role = user["role"]
                st.success("Login successful!")
                st.rerun()
            else:
                st.error("Invalid username or password")
    st.stop()

# ---------- Sidebar Navigation ----------
st.sidebar.image(settings.get("logo_url", ""), width=150)
st.sidebar.write(f"Logged in as: **{st.session_state.user['username']}** ({st.session_state.role})")

if st.sidebar.button("Logout"):
    st.session_state.authenticated = False
    st.session_state.user = None
    st.session_state.role = None
    st.rerun()

# Navigation options based on role
if st.session_state.role == "super_user":
    nav_options = ["Dashboard", "Recording", "Reports", "Vendor Manage", "Expense Head Manage", "Settings"]
else:
    nav_options = ["Dashboard", "Recording", "Reports", "Vendor Manage", "Expense Head Manage"]

page = st.sidebar.radio("Navigation", nav_options, index=nav_options.index(st.session_state.page) if st.session_state.page in nav_options else 0)
st.session_state.page = page

# ---------- DASHBOARD ----------
if page == "Dashboard":
    st.header("📊 Dashboard")
    col1, col2 = st.columns([2,1])
    with col1:
        selected_date = st.date_input("Select Date", value=date.today())
    try:
        shifts_resp = supabase.table("shifts").select("id").eq("date", selected_date.isoformat()).execute()
        shift_ids = [s["id"] for s in shifts_resp.data]
        if shift_ids:
            txns = supabase.table("transactions").select("*").in_("shift_id", shift_ids).execute().data
        else:
            txns = []
    except:
        txns = []
    sales = sum(t["amount"] for t in txns if t["type"] == "sale")
    returns = sum(t["amount"] for t in txns if t["type"] == "return")
    expenses = sum(t["amount"] for t in txns if t["type"] == "expense" and t.get("source") == "sales")
    vendor_payments = sum(t["amount"] for t in txns if t["type"] == "vendor_payment" and t.get("source") == "sales")
    purchases = sum(t["amount"] for t in txns if t["type"] == "purchase" and t.get("source") == "sales")
    withdrawals = sum(t["amount"] for t in txns if t["type"] == "withdrawal")
    net_cash = sales - returns - expenses - vendor_payments - purchases - withdrawals
    try:
        last_closed = supabase.table("shifts").select("*").eq("status", "closed").order("created_at", desc=True).limit(1).execute()
        current_cash = last_closed.data[0]["actual_closing"] if last_closed.data else 0.0
    except:
        current_cash = 0.0
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Sales", f"₹{sales:.2f}")
    col2.metric("Returns", f"₹{returns:.2f}")
    col3.metric("Expenses (cash)", f"₹{expenses:.2f}")
    col4.metric("Vendor Payments", f"₹{vendor_payments:.2f}")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Purchases (cash)", f"₹{purchases:.2f}")
    col2.metric("Withdrawals", f"₹{withdrawals:.2f}")
    col3.metric("Net Cash Flow", f"₹{net_cash:.2f}")
    col4.metric("Current Cash in Hand", f"₹{current_cash:.2f}")

# ---------- RECORDING ----------
elif page == "Recording":
    st.header("📝 Shift Recording")
    rec_date = st.date_input("Select Date", value=date.today())
    shift_tab = st.tabs(["Morning", "Evening", "Night"])
    for idx, shift_name in enumerate(["Morning", "Evening", "Night"]):
        with shift_tab[idx]:
            shift = get_today_shift(rec_date, shift_name)
            if shift is None:
                st.error("Could not load shift. Check connection.")
                continue
            st.subheader(f"{shift_name} Shift - {rec_date}")
            st.write(f"Opening Cash: ₹{shift['opening_cash']:.2f}")
            expected = compute_expected_cash(shift)
            st.info(f"Expected Closing Cash: ₹{expected:.2f}")
            with st.expander("➕ Add Sale"):
                with st.form(f"sale_{shift_name}"):
                    amt = st.number_input("Amount", min_value=0.0, format="%.2f", key=f"sale_amt_{shift_name}")
                    desc = st.text_area("Description", key=f"sale_desc_{shift_name}")
                    if st.form_submit_button("Add Sale"):
                        data = {"shift_id": shift["id"], "type": "sale", "amount": amt, "description": desc}
                        try:
                            supabase.table("transactions").insert(data).execute()
                            st.success("Sale added!")
                            st.rerun()
                        except:
                            add_pending_op("transactions", data)
                            st.warning("Offline: sale will be saved when connection resumes.")
                            st.rerun()
            with st.expander("💰 Add Expense (Multiple)"):
                if f"expense_rows_{shift_name}" not in st.session_state:
                    st.session_state[f"expense_rows_{shift_name}"] = [0]
                expense_heads = get_active_expense_heads()
                head_options = {h["id"]: h["name"] for h in expense_heads}
                for i in st.session_state[f"expense_rows_{shift_name}"]:
                    cols = st.columns([3,2,2,3,1])
                    with cols[0]:
                        head_id = st.selectbox("Head", options=list(head_options.keys()), format_func=lambda x: head_options[x], key=f"exp_head_{shift_name}_{i}")
                    with cols[1]:
                        amt = st.number_input("Amount", min_value=0.0, format="%.2f", key=f"exp_amt_{shift_name}_{i}")
                    with cols[2]:
                        source = st.selectbox("Source", ["sales", "jaib"], key=f"exp_src_{shift_name}_{i}")
                    with cols[3]:
                        desc = st.text_input("Description", key=f"exp_desc_{shift_name}_{i}")
                    with cols[4]:
                        if st.button("❌", key=f"exp_del_{shift_name}_{i}"):
                            st.session_state[f"expense_rows_{shift_name}"].remove(i)
                            st.rerun()
                if st.button("➕ Add another expense", key=f"exp_add_{shift_name}"):
                    new_idx = max(st.session_state[f"expense_rows_{shift_name}"]) + 1 if st.session_state[f"expense_rows_{shift_name}"] else 0
                    st.session_state[f"expense_rows_{shift_name}"].append(new_idx)
                    st.rerun()
                if st.button(f"Submit All Expenses for {shift_name}", key=f"exp_submit_{shift_name}"):
                    for i in st.session_state[f"expense_rows_{shift_name}"]:
                        head_id = st.session_state.get(f"exp_head_{shift_name}_{i}")
                        amt = st.session_state.get(f"exp_amt_{shift_name}_{i}")
                        source = st.session_state.get(f"exp_src_{shift_name}_{i}")
                        desc = st.session_state.get(f"exp_desc_{shift_name}_{i}", "")
                        if head_id and amt and source:
                            data = {
                                "shift_id": shift["id"],
                                "type": "expense",
                                "expense_head_id": head_id,
                                "amount": amt,
                                "source": source,
                                "description": desc
                            }
                            try:
                                supabase.table("transactions").insert(data).execute()
                            except:
                                add_pending_op("transactions", data)
                    st.success("Expenses submitted!")
                    st.session_state[f"expense_rows_{shift_name}"] = [0]
                    st.rerun()
            with st.expander("💵 Vendor Payment"):
                with st.form(f"vendor_payment_{shift_name}"):
                    vendors = get_active_vendors()
                    vendor_options = {v["id"]: v["name"] for v in vendors}
                    vendor_id = st.selectbox("Vendor", options=list(vendor_options.keys()), format_func=lambda x: vendor_options[x], key=f"vp_vendor_{shift_name}")
                    amt = st.number_input("Amount", min_value=0.0, format="%.2f", key=f"vp_amt_{shift_name}")
                    source = st.selectbox("Source", ["sales", "jaib"], key=f"vp_src_{shift_name}")
                    method = st.text_input("Payment Method (optional)", key=f"vp_method_{shift_name}")
                    desc = st.text_area("Description (optional)", key=f"vp_desc_{shift_name}")
                    if st.form_submit_button("Add Payment"):
                        data = {
                            "shift_id": shift["id"],
                            "type": "vendor_payment",
                            "vendor_id": vendor_id,
                            "amount": amt,
                            "source": source,
                            "payment_method": method,
                            "description": desc
                        }
                        try:
                            supabase.table("transactions").insert(data).execute()
                            st.success("Payment added!")
                            st.rerun()
                        except:
                            add_pending_op("transactions", data)
                            st.warning("Offline: payment will be saved later.")
            with st.expander("🛒 Purchase"):
                with st.form(f"purchase_{shift_name}"):
                    vendors = get_active_vendors()
                    vendor_options = {v["id"]: v["name"] for v in vendors}
                    vendor_id = st.selectbox("Vendor", options=list(vendor_options.keys()), format_func=lambda x: vendor_options[x], key=f"pur_vendor_{shift_name}")
                    amt = st.number_input("Amount", min_value=0.0, format="%.2f", key=f"pur_amt_{shift_name}")
                    source = st.selectbox("Source", ["sales", "jaib", "credit"], key=f"pur_src_{shift_name}")
                    desc = st.text_area("Description (optional)", key=f"pur_desc_{shift_name}")
                    if st.form_submit_button("Add Purchase"):
                        data = {
                            "shift_id": shift["id"],
                            "type": "purchase",
                            "vendor_id": vendor_id,
                            "amount": amt,
                            "source": source,
                            "description": desc
                        }
                        try:
                            supabase.table("transactions").insert(data).execute()
                            st.success("Purchase added!")
                            st.rerun()
                        except:
                            add_pending_op("transactions", data)
                            st.warning("Offline: purchase will be saved later.")
            with st.expander("🏧 Withdrawal"):
                with st.form(f"withdrawal_{shift_name}"):
                    amt = st.number_input("Amount", min_value=0.0, format="%.2f", key=f"with_amt_{shift_name}")
                    reason = st.text_area("Reason", key=f"with_reason_{shift_name}")
                    if st.form_submit_button("Add Withdrawal"):
                        data = {
                            "shift_id": shift["id"],
                            "type": "withdrawal",
                            "amount": amt,
                            "description": reason
                        }
                        try:
                            supabase.table("transactions").insert(data).execute()
                            st.success("Withdrawal added!")
                            st.rerun()
                        except:
                            add_pending_op("transactions", data)
                            st.warning("Offline: withdrawal will be saved later.")
            with st.expander("🔄 Return"):
                with st.form(f"return_{shift_name}"):
                    amt = st.number_input("Amount", min_value=0.0, format="%.2f", key=f"ret_amt_{shift_name}")
                    reason = st.text_area("Reason", key=f"ret_reason_{shift_name}")
                    if st.form_submit_button("Add Return"):
                        data = {
                            "shift_id": shift["id"],
                            "type": "return",
                            "amount": amt,
                            "description": reason
                        }
                        try:
                            supabase.table("transactions").insert(data).execute()
                            st.success("Return added!")
                            st.rerun()
                        except:
                            add_pending_op("transactions", data)
                            st.warning("Offline: return will be saved later.")
            if shift["status"] == "open":
                st.divider()
                st.subheader("Close Shift")
                actual = st.number_input("Actual Cash in Hand", min_value=0.0, format="%.2f", key=f"actual_{shift_name}")
                if st.button(f"Close {shift_name} Shift", key=f"close_{shift_name}"):
                    if close_shift(shift["id"], actual):
                        st.success("Shift closed!")
                        st.rerun()
                    else:
                        st.error("Failed to close shift.")

# ---------- REPORTS ----------
elif page == "Reports":
    st.header("📈 Reports")
    rep_tab = st.tabs(["Shift Report", "Expense Report", "Vendor Report", "Personal Ledger", "Profit & Loss"])
    # Shift Report
    with rep_tab[0]:
        st.subheader("Shift Report")
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Start Date", value=date.today() - timedelta(days=7), key="shift_start")
        with col2:
            end_date = st.date_input("End Date", value=date.today(), key="shift_end")
        shift_filter = st.selectbox("Select Shift", ["All", "Morning", "Evening", "Night"], key="shift_filter")
        if st.button("Generate Shift Report", key="gen_shift"):
            try:
                query = supabase.table("shifts").select("*").gte("date", start_date.isoformat()).lte("date", end_date.isoformat())
                if shift_filter != "All":
                    query = query.eq("shift", shift_filter)
                shifts = query.execute().data
                if not shifts:
                    st.warning("No shifts found.")
                else:
                    report_data = []
                    total_sales = total_expenses = total_vendor_payments = total_withdrawals = total_shortage = 0.0
                    for s in shifts:
                        txns = supabase.table("transactions").select("*").eq("shift_id", s["id"]).execute().data
                        sales = sum(t["amount"] for t in txns if t["type"] == "sale")
                        expenses = sum(t["amount"] for t in txns if t["type"] == "expense" and t.get("source") == "sales")
                        vendor_payments = sum(t["amount"] for t in txns if t["type"] == "vendor_payment" and t.get("source") == "sales")
                        withdrawals = sum(t["amount"] for t in txns if t["type"] == "withdrawal")
                        shortage = s.get("shortage", 0.0)
                        expected = s.get("expected_closing", 0.0)
                        actual = s.get("actual_closing", 0.0)
                        report_data.append([
                            s["date"], s["shift"],
                            f"{sales:.2f}", f"{expenses:.2f}", f"{vendor_payments:.2f}",
                            f"{withdrawals:.2f}", f"{shortage:.2f}",
                            f"{expected:.2f}", f"{actual:.2f}"
                        ])
                        total_sales += sales
                        total_expenses += expenses
                        total_vendor_payments += vendor_payments
                        total_withdrawals += withdrawals
                        total_shortage += shortage
                    report_data.append([
                        "GRAND TOTAL", "", f"{total_sales:.2f}", f"{total_expenses:.2f}",
                        f"{total_vendor_payments:.2f}", f"{total_withdrawals:.2f}",
                        f"{total_shortage:.2f}", "", ""
                    ])
                    columns = ["Date", "Shift", "Sales", "Expenses", "Vendor Pmts", "Withdrawals", "Shortage", "Expected", "Actual"]
                    df = pd.DataFrame(report_data, columns=columns)
                    st.dataframe(df)
                    # CSV download
                    csv = df.to_csv(index=False).encode('utf-8')
                    st.download_button("Download CSV", data=csv, file_name="shift_report.csv", mime="text/csv")
                    # PDF download
                    shop = get_shop_details()
                    date_range_str = f"{start_date} to {end_date}"
                    title = f"Shift Report ({shift_filter})"
                    pdf_bytes = generate_pdf(title, date_range_str, columns, report_data, shop)
                    st.download_button("Download PDF", data=pdf_bytes, file_name="shift_report.pdf", mime="application/pdf")
            except Exception as e:
                st.error(f"Error: {e}")
    # Expense Report
    with rep_tab[1]:
        st.subheader("Expense Report")
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Start Date", value=date.today() - timedelta(days=7), key="exp_start")
        with col2:
            end_date = st.date_input("End Date", value=date.today(), key="exp_end")
        try:
            heads = supabase.table("expense_heads").select("*").execute().data
            head_options = {0: "All Heads"}
            head_options.update({h["id"]: h["name"] for h in heads})
            selected_head = st.selectbox("Select Expense Head", options=list(head_options.keys()), format_func=lambda x: head_options[x], key="exp_head")
        except:
            st.error("Could not load expense heads.")
            selected_head = 0
        if st.button("Generate Expense Report", key="gen_exp"):
            try:
                query = supabase.table("transactions").select("*, expense_heads(name)").eq("type", "expense").gte("created_at", start_date.isoformat()).lte("created_at", (end_date + timedelta(days=1)).isoformat())
                if selected_head != 0:
                    query = query.eq("expense_head_id", selected_head)
                txns = query.execute().data
                if not txns:
                    st.warning("No expenses found.")
                else:
                    report_data = []
                    for t in txns:
                        report_data.append([
                            t["created_at"][:10],
                            t["expense_heads"]["name"] if t["expense_heads"] else "Unknown",
                            t.get("description", ""),
                            f"{t['amount']:.2f}"
                        ])
                    columns = ["Date", "Expense Head", "Description", "Amount"]
                    df = pd.DataFrame(report_data, columns=columns)
                    st.dataframe(df)
                    csv = df.to_csv(index=False).encode('utf-8')
                    st.download_button("Download CSV", data=csv, file_name="expense_report.csv", mime="text/csv")
                    shop = get_shop_details()
                    date_range_str = f"{start_date} to {end_date}"
                    head_name = head_options[selected_head] if selected_head != 0 else "All Heads"
                    title = f"Expense Report ({head_name})"
                    pdf_bytes = generate_pdf(title, date_range_str, columns, report_data, shop)
                    st.download_button("Download PDF", data=pdf_bytes, file_name="expense_report.pdf", mime="application/pdf")
            except Exception as e:
                st.error(f"Error: {e}")
    # Vendor Report
    with rep_tab[2]:
        st.subheader("Vendor Report")
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Start Date", value=date.today() - timedelta(days=7), key="ven_start")
        with col2:
            end_date = st.date_input("End Date", value=date.today(), key="ven_end")
        try:
            vendors = supabase.table("vendors").select("*").execute().data
            vendor_options = {0: "All Vendors"}
            vendor_options.update({v["id"]: v["name"] for v in vendors})
            selected_vendor = st.selectbox("Select Vendor", options=list(vendor_options.keys()), format_func=lambda x: vendor_options[x], key="ven_vendor")
        except:
            st.error("Could not load vendors.")
            selected_vendor = 0
        if st.button("Generate Vendor Report", key="gen_ven"):
            try:
                query = supabase.table("transactions").select("*, vendors(name)").in_("type", ["purchase", "vendor_payment", "return"]).gte("created_at", start_date.isoformat()).lte("created_at", (end_date + timedelta(days=1)).isoformat())
                if selected_vendor != 0:
                    query = query.eq("vendor_id", selected_vendor)
                txns = query.execute().data
                if not txns:
                    st.warning("No vendor transactions found.")
                else:
                    txns.sort(key=lambda x: x["created_at"])
                    balance = 0
                    report_data = []
                    for t in txns:
                        if t["type"] == "purchase":
                            if t.get("source") == "credit":
                                balance += t["amount"]
                        elif t["type"] == "vendor_payment":
                            balance -= t["amount"]
                        elif t["type"] == "return":
                            balance -= t["amount"]
                        report_data.append([
                            t["created_at"][:10],
                            t["type"].replace("_", " ").title(),
                            t.get("description", ""),
                            f"{t['amount']:.2f}",
                            f"{balance:.2f}"
                        ])
                    columns = ["Date", "Type", "Description", "Amount", "Balance"]
                    df = pd.DataFrame(report_data, columns=columns)
                    st.dataframe(df)
                    csv = df.to_csv(index=False).encode('utf-8')
                    st.download_button("Download CSV", data=csv, file_name="vendor_report.csv", mime="text/csv")
                    shop = get_shop_details()
                    date_range_str = f"{start_date} to {end_date}"
                    vendor_name = vendor_options[selected_vendor] if selected_vendor != 0 else "All Vendors"
                    title = f"Vendor Report ({vendor_name})"
                    pdf_bytes = generate_pdf(title, date_range_str, columns, report_data, shop)
                    st.download_button("Download PDF", data=pdf_bytes, file_name="vendor_report.pdf", mime="application/pdf")
            except Exception as e:
                st.error(f"Error: {e}")
    # Personal Ledger
    with rep_tab[3]:
        st.subheader("Personal Ledger")
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Start Date", value=date.today() - timedelta(days=7), key="per_start")
        with col2:
            end_date = st.date_input("End Date", value=date.today(), key="per_end")
        if st.button("Generate Personal Ledger", key="gen_per"):
            try:
                jaib_txns = supabase.table("transactions").select("*, expense_heads(name), vendors(name)").eq("source", "jaib").gte("created_at", start_date.isoformat()).lte("created_at", (end_date + timedelta(days=1)).isoformat()).execute().data
                withdrawal_txns = supabase.table("transactions").select("*").eq("type", "withdrawal").gte("created_at", start_date.isoformat()).lte("created_at", (end_date + timedelta(days=1)).isoformat()).execute().data
                all_txns = jaib_txns + withdrawal_txns
                all_txns.sort(key=lambda x: x["created_at"])
                if not all_txns:
                    st.warning("No personal transactions found.")
                else:
                    balance = 0
                    report_data = []
                    for t in all_txns:
                        if t["type"] == "withdrawal":
                            invest = 0
                            withdraw = t["amount"]
                            desc = f"Withdrawal: {t.get('description', '')}"
                            balance -= withdraw
                        else:
                            invest = t["amount"]
                            withdraw = 0
                            if t["type"] == "expense":
                                head_name = t["expense_heads"]["name"] if t["expense_heads"] else "Unknown"
                                desc = f"Expense ({head_name}): {t.get('description', '')}"
                            elif t["type"] == "vendor_payment":
                                vendor_name = t["vendors"]["name"] if t["vendors"] else "Unknown"
                                desc = f"Vendor Payment ({vendor_name}): {t.get('description', '')}"
                            elif t["type"] == "purchase":
                                vendor_name = t["vendors"]["name"] if t["vendors"] else "Unknown"
                                desc = f"Purchase ({vendor_name}): {t.get('description', '')}"
                            else:
                                desc = t.get("description", "")
                            balance += invest
                        report_data.append([
                            t["created_at"][:10],
                            desc,
                            f"{invest:.2f}" if invest else "",
                            f"{withdraw:.2f}" if withdraw else "",
                            f"{balance:.2f}"
                        ])
                    columns = ["Date", "Description", "Invest", "Withdraw", "Balance"]
                    df = pd.DataFrame(report_data, columns=columns)
                    def color_balance(val):
                        try:
                            num = float(val)
                            color = 'red' if num < 0 else 'green'
                            return f'color: {color}'
                        except:
                            return ''
                    styled_df = df.style.applymap(color_balance, subset=['Balance'])
                    st.dataframe(styled_df)
                    csv = df.to_csv(index=False).encode('utf-8')
                    st.download_button("Download CSV", data=csv, file_name="personal_ledger.csv", mime="text/csv")
                    shop = get_shop_details()
                    date_range_str = f"{start_date} to {end_date}"
                    title = "Personal Ledger"
                    pdf_bytes = generate_pdf(title, date_range_str, columns, report_data, shop)
                    st.download_button("Download PDF", data=pdf_bytes, file_name="personal_ledger.pdf", mime="application/pdf")
            except Exception as e:
                st.error(f"Error: {e}")
    # Profit & Loss
    with rep_tab[4]:
        st.subheader("Profit & Loss")
        col1, col2 = st.columns(2)
        with col1:
            pl_start = st.date_input("Start Date", value=date.today() - timedelta(days=30), key="pl_start")
        with col2:
            pl_end = st.date_input("End Date", value=date.today(), key="pl_end")
        cogs = st.number_input("COGS (Cost of Goods Sold)", min_value=0.0, format="%.2f", value=0.0)
        if st.button("Calculate P&L", key="calc_pl"):
            try:
                # Get all transactions in date range
                shifts_resp = supabase.table("shifts").select("id").gte("date", pl_start.isoformat()).lte("date", pl_end.isoformat()).execute()
                shift_ids = [s["id"] for s in shifts_resp.data]
                if shift_ids:
                    txns = supabase.table("transactions").select("*").in_("shift_id", shift_ids).execute().data
                else:
                    txns = []
                sales = sum(t["amount"] for t in txns if t["type"] == "sale")
                returns = sum(t["amount"] for t in txns if t["type"] == "return")
                net_sales = sales - returns
                expenses = sum(t["amount"] for t in txns if t["type"] == "expense" and t.get("source") == "sales")
                gross_profit = net_sales - cogs
                net_profit = gross_profit - expenses
                col1, col2, col3 = st.columns(3)
                col1.metric("Net Sales", f"₹{net_sales:.2f}")
                col2.metric("COGS", f"₹{cogs:.2f}")
                col3.metric("Gross Profit", f"₹{gross_profit:.2f}")
                col1.metric("Expenses", f"₹{expenses:.2f}")
                col2.metric("Net Profit", f"₹{net_profit:.2f}")
                # PDF report
                shop = get_shop_details()
                date_range_str = f"{pl_start} to {pl_end}"
                pdf = FPDF()
                pdf.add_page()
                pdf.set_font("Arial", "B", 16)
                pdf.cell(0, 10, shop["name"], ln=1, align="C")
                pdf.set_font("Arial", "", 10)
                pdf.cell(0, 6, shop["address"], ln=1, align="C")
                pdf.cell(0, 6, "Profit & Loss Statement", ln=1, align="C")
                pdf.cell(0, 6, f"Date Range: {date_range_str}", ln=1, align="C")
                pdf.ln(10)
                pdf.set_font("Arial", "B", 12)
                pdf.cell(0, 10, f"Net Sales: ₹{net_sales:.2f}", ln=1)
                pdf.cell(0, 10, f"COGS: ₹{cogs:.2f}", ln=1)
                pdf.cell(0, 10, f"Gross Profit: ₹{gross_profit:.2f}", ln=1)
                pdf.cell(0, 10, f"Expenses: ₹{expenses:.2f}", ln=1)
                pdf.cell(0, 10, f"Net Profit: ₹{net_profit:.2f}", ln=1)
                pdf_bytes = pdf.output(dest='S').encode('latin1')
                st.download_button("Download PDF", data=pdf_bytes, file_name="profit_loss.pdf", mime="application/pdf")
            except Exception as e:
                st.error(f"Error: {e}")

# ---------- VENDOR MANAGE ----------
elif page == "Vendor Manage":
    st.header("🏢 Manage Vendors")
    show_inactive = st.checkbox("Show inactive vendors")
    try:
        query = supabase.table("vendors").select("*")
        if not show_inactive:
            query = query.eq("is_active", True)
        vendors = query.execute().data
        for v in vendors:
            col1, col2, col3, col4, col5 = st.columns([3,1,1,1,1])
            col1.write(v["name"])
            col2.write("Active" if v["is_active"] else "Inactive")
            if col3.button("Edit", key=f"edit_{v['id']}"):
                st.session_state[f"edit_vendor_{v['id']}"] = True
            if col4.button("Toggle Active", key=f"toggle_{v['id']}"):
                new_status = not v["is_active"]
                try:
                    supabase.table("vendors").update({"is_active": new_status}).eq("id", v["id"]).execute()
                    st.rerun()
                except:
                    add_pending_op("vendors", {"id": v["id"], "is_active": new_status}, "update")
                    st.warning("Offline: update pending")
            if col5.button("Delete", key=f"del_{v['id']}"):
                try:
                    supabase.table("vendors").delete().eq("id", v["id"]).execute()
                    st.rerun()
                except:
                    add_pending_op("vendors", {"id": v["id"]}, "delete")
                    st.warning("Offline: delete pending")
        with st.form("new_vendor"):
            new_name = st.text_input("Vendor Name")
            if st.form_submit_button("Add Vendor"):
                if new_name:
                    try:
                        supabase.table("vendors").insert({"name": new_name, "is_active": True}).execute()
                        st.rerun()
                    except:
                        add_pending_op("vendors", {"name": new_name, "is_active": True})
                        st.warning("Offline: vendor will be added later")
    except Exception as e:
        st.error(f"Could not load vendors: {e}")

# ---------- EXPENSE HEAD MANAGE ----------
elif page == "Expense Head Manage":
    st.header("📋 Manage Expense Heads")
    show_inactive = st.checkbox("Show inactive heads", key="exp_show_inactive")
    try:
        query = supabase.table("expense_heads").select("*")
        if not show_inactive:
            query = query.eq("is_active", True)
        heads = query.execute().data
        for h in heads:
            col1, col2, col3, col4, col5 = st.columns([3,1,1,1,1])
            col1.write(h["name"])
            col2.write("Active" if h["is_active"] else "Inactive")
            if col3.button("Edit", key=f"edit_head_{h['id']}"):
                st.session_state[f"edit_head_{h['id']}"] = True
            if col4.button("Toggle Active", key=f"toggle_head_{h['id']}"):
                new_status = not h["is_active"]
                try:
                    supabase.table("expense_heads").update({"is_active": new_status}).eq("id", h["id"]).execute()
                    st.rerun()
                except:
                    add_pending_op("expense_heads", {"id": h["id"], "is_active": new_status}, "update")
            if col5.button("Delete", key=f"del_head_{h['id']}"):
                try:
                    supabase.table("expense_heads").delete().eq("id", h["id"]).execute()
                    st.rerun()
                except:
                    add_pending_op("expense_heads", {"id": h["id"]}, "delete")
        with st.form("new_head"):
            new_name = st.text_input("Expense Head Name")
            if st.form_submit_button("Add Head"):
                if new_name:
                    try:
                        supabase.table("expense_heads").insert({"name": new_name, "is_active": True}).execute()
                        st.rerun()
                    except:
                        add_pending_op("expense_heads", {"name": new_name, "is_active": True})
    except Exception as e:
        st.error(f"Could not load expense heads: {e}")

# ---------- SETTINGS (Super User only) ----------
elif page == "Settings":
    if st.session_state.role != "super_user":
        st.error("Access denied. Super user only.")
        st.stop()
    st.header("⚙️ Settings")
    tab1, tab2, tab3 = st.tabs(["User Management", "App Settings", "Styling"])
    with tab1:
        st.subheader("Manage Users")
        try:
            users = supabase.table("users").select("*").execute().data
            for u in users:
                col1, col2, col3, col4 = st.columns([3,2,1,1])
                col1.write(u["username"])
                col2.write(u["role"])
                if u["username"] != st.session_state.user["username"]:
                    if col4.button("🗑️", key=f"del_user_{u['id']}"):
                        supabase.table("users").delete().eq("id", u["id"]).execute()
                        st.rerun()
                else:
                    col4.write("(you)")
            with st.form("add_user"):
                new_user = st.text_input("Username")
                new_pass = st.text_input("Password", type="password")
                new_role = st.selectbox("Role", ["owner", "super_user"])
                if st.form_submit_button("Create User"):
                    if new_user and new_pass:
                        existing = supabase.table("users").select("*").eq("username", new_user).execute()
                        if existing.data:
                            st.error("Username exists.")
                        else:
                            supabase.table("users").insert({"username": new_user, "password": new_pass, "role": new_role}).execute()
                            st.success("User created!")
                            st.rerun()
        except Exception as e:
            st.error(f"Could not load users: {e}")
    with tab2:
        st.subheader("Application Settings")
        settings = get_settings()
        with st.form("settings_form"):
            shop_name = st.text_input("Shop Name", value=settings.get("shop_name", ""))
            shop_address = st.text_area("Shop Address", value=settings.get("shop_address", ""))
            logo_url = st.text_input("Logo URL", value=settings.get("logo_url", ""))
            pdf_css = st.text_area("PDF Styling (CSS)", value=settings.get("pdf_css", ""), height=150)
            if st.form_submit_button("Save Settings"):
                update_setting("shop_name", shop_name)
                update_setting("shop_address", shop_address)
                update_setting("logo_url", logo_url)
                update_setting("pdf_css", pdf_css)
                st.success("Settings saved!")
                st.rerun()
    with tab3:
        st.subheader("App Styling (Custom CSS)")
        settings = get_settings()
        with st.form("app_css_form"):
            app_css = st.text_area("App CSS", value=settings.get("app_css", ""), height=200)
            if st.form_submit_button("Update App CSS"):
                update_setting("app_css", app_css)
                st.success("App CSS updated!")
                st.rerun()

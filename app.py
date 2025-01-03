import os
import io
import csv
import json
import logging
from datetime import datetime, timedelta
import random
import tempfile

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_file,
    send_from_directory,   # <--- we’ll use this to serve files
    flash,
    make_response,
    jsonify,
    session
)
from werkzeug.utils import secure_filename

# For .env loading
from dotenv import load_dotenv

# For Twilio
from twilio.rest import Client

# For static PNG charts
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# For optional pandas/plotly
import pandas as pd
import plotly.express as px
import plotly.io as pio

load_dotenv()  # Load .env

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = 'SOME_LONG_SECRET_KEY'

# ------------------ Twilio Setup ------------------
TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER")
TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH  = os.getenv("TWILIO_AUTH_TOKEN")
twilio_client = Client(TWILIO_SID, TWILIO_AUTH)

ALLOWED_PHONE_NUMBERS = {
    "+43 670 359 66 14",
    "+43 699 10503659"
}

# We'll store the "last code sent" in memory for demonstration.
# In production, you'd store this in a DB or ephemeral store with expiration.
# Format: { "<phone>": { "code": "123456", "timestamp": ... } }
phone_code_map = {}

# ------------------ In-Memory Data -----------------
ALLOWED_EXTENSIONS = {'csv'}

transactions = []  # Each: { <CSV fields>, 'DetectedCategoryId': int or None }
categories = []
groups = []
lists_data = []

next_category_id = 1
next_group_id = 1
next_list_id = 1

active_json_file = None

# Optional: track parse status for each CSV
parsed_csv_files = {}

CHARTS_FOLDER = os.path.join(os.getcwd(), 'charts')  # e.g. "charts/"
os.makedirs(CHARTS_FOLDER, exist_ok=True)

UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ------------------ Utility Functions -----------------

    
    
def reclassify_all_transactions_in_memory():
    for trx in transactions:
        cid = classify_transaction(trx)
        trx['DetectedCategoryId'] = cid

def classify_transaction(trx):
    text = (
        (trx.get('Buchungstext','') + ' ' +
         trx.get('Umsatztext','') + ' ' +
         trx.get('Name des Partners','') + ' ' +
         trx.get('Verwendungszweck','')).lower()
    )
    for cat in categories:
        for rule in cat['rules']:
            if rule.lower() in text:
                logger.debug(f"Transaction matched category {cat['name']} by rule '{rule}'")
                return cat['id']
    return None

def allowed_file(filename):
    logger.debug(f"Checking file extension for: {filename}")
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def build_transaction_key(trx):
    return {
        'Buchungsdatum': trx.get('Buchungsdatum', ''),
        'Buchungstext':  trx.get('Buchungstext', ''),
        'Betrag':        trx.get('Betrag', 0.0)
    }

def find_trx_index_by_key(key_dict):
    for i, trx in enumerate(transactions):
        if (
            trx.get('Buchungsdatum','') == key_dict.get('Buchungsdatum','') and
            trx.get('Buchungstext','')  == key_dict.get('Buchungstext','')  and
            abs(trx.get('Betrag', 0.0) - key_dict.get('Betrag', 0.0)) < 1e-9
        ):
            return i
    return -1

def parse_csv_and_store(file_stream, filename=""):
    logger.debug(f"Parsing CSV file stream for: {filename}")
    raw_data = file_stream.read()
    try:
        content_decoded = raw_data.decode('utf-8')
        logger.debug("Decoded as UTF-8.")
    except UnicodeDecodeError as e:
        logger.warning(f"Failed UTF-8 decoding ({e}). Fallback to latin-1.")
        content_decoded = raw_data.decode('latin-1', errors='replace')

    reader = csv.reader(io.StringIO(content_decoded), delimiter=';')
    headers = None

    row_count = 0
    for idx, row in enumerate(reader):
        if not row or all(not cell.strip() for cell in row):
            continue
        if headers is None:
            headers = row
            logger.debug(f"CSV headers: {headers}")
            continue

        row_data = {}
        for h_i, header in enumerate(headers):
            row_data[header] = row[h_i] if h_i < len(row) else ''

        if 'Betrag' in row_data:
            val = row_data['Betrag'].replace('\t','').replace(',','.')
            try:
                row_data['Betrag'] = float(val)
            except:
                row_data['Betrag'] = 0.0

        row_data['DetectedCategoryId'] = classify_transaction(row_data)
        transactions.append(row_data)
        row_count += 1

    logger.debug(f"Finished parsing {filename}. Stored {row_count} rows.")

def get_category_by_id(cat_id):
    for c in categories:
        if c['id'] == cat_id:
            return c
    return None

def get_group_by_id(g_id):
    for g in groups:
        if g['id'] == g_id:
            return g
    return None

def get_list_by_id(l_id):
    for l in lists_data:
        if l['id'] == l_id:
            return l
    return None

def is_expense(trx):
    amt = trx.get('Betrag', 0.0)
    return (amt < 0)

def compute_refund_status(trx_index):
    for lst in lists_data:
        if lst.get('refund_list') and (trx_index in lst['transaction_ids']):
            return True
    return False

def is_in_any_list(trx_index):
    for lst in lists_data:
        if trx_index in lst['transaction_ids']:
            return True
    return False

def get_trx_amounts_for_category(cat_id):
    total_overall = 0.0
    total_excluding_lists = 0.0
    total_list_items = 0.0
    refundable_sum = 0.0

    for idx, trx in enumerate(transactions):
        if is_expense(trx) and trx.get('DetectedCategoryId') == cat_id:
            amt = trx['Betrag']
            total_overall += amt

            if is_in_any_list(idx):
                total_list_items += amt
            else:
                total_excluding_lists += amt

            if compute_refund_status(idx):
                refundable_sum += amt

    after_refund = total_overall - refundable_sum
    return (total_overall, refundable_sum, after_refund,
            total_excluding_lists, total_list_items)

def compute_global_expenses():
    total = 0.0
    refundable = 0.0
    for idx, trx in enumerate(transactions):
        if is_expense(trx):
            amt = trx.get('Betrag',0.0)
            total += amt
            if compute_refund_status(idx):
                refundable += amt
    return (total, refundable, total - refundable)

def compute_global_income():
    total_inc = 0.0
    for trx in transactions:
        if trx.get('Betrag',0.0) >= 0:
            total_inc += trx['Betrag']
    return total_inc

def list_files_in_uploads(extension=".csv"):
    if not os.path.exists(UPLOAD_FOLDER):
        return []
    all_files = os.listdir(UPLOAD_FOLDER)
    filtered = [f for f in all_files if f.lower().endswith(extension)]
    return sorted(filtered)

def build_transactions_data():
    results = []
    for idx, trx in enumerate(transactions):
        cat = get_category_by_id(trx.get('DetectedCategoryId'))
        cat_name = cat['name'] if cat else "UNK"
        cat_color = cat['color'] if cat else "#dddddd"
        results.append({
            "idx": idx,
            "buchungstext": trx.get('Buchungstext',''),
            "umsatztext": trx.get('Umsatztext',''),
            "partner": trx.get('Name des Partners',''),
            "betrag": trx.get('Betrag',0.0),
            "buchungsdatum": trx.get('Buchungsdatum',''),
            "cat_id": cat['id'] if cat else None,
            "cat_name": cat_name,
            "cat_color": cat_color
        })
    return results


@app.route('/charts/<filename>')
def serve_chart_file(filename):
    """
    Serve a pre-rendered PNG from the /charts/ folder.
    E.g. GET /charts/bar_chart.png or /charts/group_chart.png
    """
    return send_from_directory(CHARTS_FOLDER, filename)


# ------------------- New: Protect all routes with @before_request ---------------
@app.before_first_request
def pre_render_charts():
    """
    This will run once (the first time the server receives any request).
    We'll generate bar_chart.png and group_chart.png and store them in /charts/ folder.
    """

    # 1) Generate the 'bar_chart.png'
    fig, ax = plt.subplots(figsize=(6,4))

    cat_sums = {}
    cat_sums[None] = 0.0
    for c in categories:
        cat_sums[c['id']] = 0.0

    for trx in transactions:
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            cat_sums.setdefault(cid, 0.0)
            cat_sums[cid] += trx['Betrag']

    cat_names = []
    sums = []
    colors = []
    for c in categories:
        cat_names.append(c['name'])
        sums.append(cat_sums[c['id']])
        colors.append(c['color'])

    if cat_sums[None] != 0.0:
        cat_names.append("UNK")
        sums.append(cat_sums[None])
        colors.append("#dddddd")

    if len(cat_names) == 0:
        cat_names = ["No data"]
        sums = [0]
        colors = ["#999999"]

    bars = ax.bar(cat_names, sums, color=colors)
    ax.set_title("Total Negative Amount by Category (PNG)")
    ax.set_xlabel("Category")
    ax.set_ylabel("Amount")
    plt.xticks(rotation=45, ha='right')

    # Label each bar
    for rect, val in zip(bars, sums):
        height = rect.get_height()
        if val >= 0:
            ax.text(rect.get_x() + rect.get_width()/2, height, f"{val:.2f}",
                    ha='center', va='bottom', fontsize=8)
        else:
            ax.text(rect.get_x() + rect.get_width()/2, height, f"{val:.2f}",
                    ha='center', va='top', fontsize=8)

    total_val = sum(sums)
    ax.text(0.02, 0.98, f"Total: {total_val:.2f}",
            transform=ax.transAxes, ha='left', va='top', fontsize=10, color='#000000')

    plt.tight_layout()
    bar_chart_path = os.path.join(CHARTS_FOLDER, 'bar_chart.png')
    plt.savefig(bar_chart_path, format='png')
    plt.close(fig)

    # 2) Generate the 'group_chart.png'
    fig2, ax2 = plt.subplots(figsize=(6,4))

    # (similar code from your group_bar_chart_png logic)
    cat_sums = {}
    for c in categories:
        cat_sums[c['id']] = 0.0

    for trx in transactions:
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            cat_sums.setdefault(cid, 0.0)
            cat_sums[cid] += trx['Betrag']

    group_sums = {}
    for g in groups:
        group_sums[g['id']] = 0.0

    for c in categories:
        g_id = c.get('group_id')
        group_sums.setdefault(g_id, 0.0)
        group_sums[g_id] += cat_sums[c['id']]

    show_as_group = []
    for c in categories:
        if c.get('show_up_as_group'):
            show_as_group.append(("[CatAsGroup] "+c['name'], cat_sums[c['id']], c['color']))

    group_names = []
    group_vals = []
    color_list = []
    for g in groups:
        group_names.append(g['name'])
        group_vals.append(group_sums[g['id']])
        color_list.append(g['color'])

    for (lbl, val, col) in show_as_group:
        group_names.append(lbl)
        group_vals.append(val)
        color_list.append(col)

    if len(group_names) == 0:
        group_names = ["No data"]
        group_vals = [0]
        color_list = ["#999999"]

    bars2 = ax2.bar(group_names, group_vals, color=color_list)
    ax2.set_title("Total Negative Amount by Group (PNG)")
    ax2.set_xlabel("Group")
    ax2.set_ylabel("Amount")
    plt.xticks(rotation=45, ha='right')

    for rect, val in zip(bars2, group_vals):
        height = rect.get_height()
        if val >= 0:
            ax2.text(rect.get_x() + rect.get_width()/2, height, f"{val:.2f}",
                     ha='center', va='bottom', fontsize=8)
        else:
            ax2.text(rect.get_x() + rect.get_width()/2, height, f"{val:.2f}",
                     ha='center', va='top', fontsize=8)

    total_val2 = sum(group_vals)
    ax2.text(0.02, 0.98, f"Total: {total_val2:.2f}",
             transform=ax2.transAxes, ha='left', va='top', fontsize=10, color='#000000')

    plt.tight_layout()
    group_chart_path = os.path.join(CHARTS_FOLDER, 'group_chart.png')
    plt.savefig(group_chart_path, format='png')
    plt.close(fig2)

    logger.info("Pre-rendered bar_chart.png and group_chart.png in /charts/ folder.")
    
@app.before_request
def require_login():
    # Let static files, login, verify, bar_chart_png, group_bar_chart_png pass:
    if request.endpoint in (
        "login", 
        "verify", 
        "static", 
        "bar_chart_png",     # <<-- ALLOW
        "group_bar_chart_png"  # <<-- ALLOW
    ):
        return
    # If not logged in => redirect to /login
    if not session.get("phone_verified"):
        return redirect(url_for("login"))


# ------------------- Routes for phone-based login ---------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        if phone in ALLOWED_PHONE_NUMBERS:
            # Generate a random 6-digit code
            code = f"{random.randint(100000,999999)}"
            # Store it in phone_code_map
            phone_code_map[phone] = {
                "code": code,
                "timestamp": datetime.now()
            }
            # Send SMS via Twilio
            try:
                twilio_client.messages.create(
                    body=f"Your verification code is {code}",
                    from_=TWILIO_PHONE,
                    to=phone
                )
                # Store the phone in session temporarily
                session["pending_phone"] = phone
                flash("Verification code sent via SMS!", "success")
                return redirect(url_for("verify"))
            except Exception as e:
                flash(f"Error sending SMS: {e}", "danger")
                return redirect(url_for("login"))
        else:
            flash("Phone number not allowed.", "danger")
            return redirect(url_for("login"))
    return render_template("login.html")  # a simple form that asks for phone

@app.route("/verify", methods=["GET", "POST"])
def verify():
    if request.method == "POST":
        user_code = request.form.get("code", "").strip()
        phone = session.get("pending_phone", "")
        if not phone:
            flash("No phone in session. Please re-login.", "danger")
            return redirect(url_for("login"))

        # Check code
        entry = phone_code_map.get(phone, {})
        if not entry:
            flash("No code entry found. Please re-login.", "danger")
            return redirect(url_for("login"))

        if user_code == entry["code"]:
            # Mark user as verified
            session["phone_verified"] = phone
            # Clear pending from session
            session.pop("pending_phone", None)
            flash("Phone verified! Welcome!", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid code. Please try again.", "danger")
            return redirect(url_for("verify"))

    return render_template("verify.html")  # a simple form that asks for code


# ------------------- Routes --------------------

@app.route('/')
def index():
    logger.debug("Serving home page.")
    return render_template('index.html')

@app.route('/transactions')
def view_transactions():
    csv_files_on_disk = list_files_in_uploads(".csv")
    json_files_on_disk = list_files_in_uploads(".json")

    sort_mode = request.args.get('sort','lowest')

    data = []
    for idx, trx in enumerate(transactions):
        cat = get_category_by_id(trx.get('DetectedCategoryId'))
        cat_name = cat['name'] if cat else "UNK"
        cat_color = cat['color'] if cat else "#dddddd"
        data.append((trx, cat_name, cat_color, idx))

    # Sorting
    if sort_mode == 'lowest':
        data.sort(key=lambda x: x[0].get('Betrag',0.0))
    elif sort_mode == 'highest':
        data.sort(key=lambda x: x[0].get('Betrag',0.0), reverse=True)
    elif sort_mode == 'latest_date':
        def parse_date(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d")
            except:
                return datetime.min
        data.sort(key=lambda x: parse_date(x[0].get('Buchungsdatum','')), reverse=True)
    elif sort_mode == 'oldest_date':
        def parse_date(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d")
            except:
                return datetime.min
        data.sort(key=lambda x: parse_date(x[0].get('Buchungsdatum','')))

    total_exp, ref_exp, after_exp = compute_global_expenses()

    return render_template(
        'transactions.html',
        transactions_data=data,
        categories=categories,
        lists_data=lists_data,
        sort_mode=sort_mode,
        total=total_exp,
        refundable=ref_exp,
        after_refund=after_exp,
        uploaded_csv_files=csv_files_on_disk,
        uploaded_json_files=json_files_on_disk,
        active_json_file=active_json_file
    )

@app.route('/transactions/upload_csv', methods=['POST'])
def upload_csv_files():
    files = request.files.getlist('csv_files[]')
    parse_on_upload = request.form.get('parse_on_upload', '')

    if not files:
        flash("No CSV files found in request", "danger")
        return redirect(url_for('view_transactions'))

    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    for file in files:
        if not file.filename:
            flash("One of the uploaded files has an empty filename.", "danger")
            continue

        if not allowed_file(file.filename):
            flash(f"File {file.filename} not allowed (must be .csv).", "danger")
            continue

        filename = secure_filename(file.filename)
        save_path = os.path.join(UPLOAD_FOLDER, filename)
        file.seek(0)
        file.save(save_path)

        if parse_on_upload:
            file.stream.seek(0)
            parse_csv_and_store(file.stream, filename)
            parsed_csv_files[filename] = True
            flash(f"CSV file '{filename}' uploaded & parsed.", "success")
        else:
            flash(f"CSV file '{filename}' uploaded (not parsed).", "success")

    return redirect(url_for('view_transactions'))

@app.route('/ajax/csv/parse', methods=['POST'])
def ajax_parse_csv():
    data = request.get_json()
    if not data:
        return jsonify({"status":"error","message":"No JSON data"}), 400

    fname = data.get("filename","")
    if not fname:
        return jsonify({"status":"error","message":"No filename provided"}),400

    full_path = os.path.join(UPLOAD_FOLDER, fname)
    if not os.path.exists(full_path):
        return jsonify({"status":"error","message":"File not found on disk"}),404

    with open(full_path, "rb") as f:
        parse_csv_and_store(f, fname)
    parsed_csv_files[fname] = True

    updated_tx_data = build_transactions_data()
    return jsonify({
        "status":"ok",
        "message":f"Parsed CSV file '{fname}'",
        "transactions": updated_tx_data
    })

@app.route('/ajax/csv/delete', methods=['POST'])
def ajax_delete_csv():
    data = request.get_json()
    if not data:
        return jsonify({"status":"error","message":"No JSON data"}),400

    fname = data.get("filename","")
    if not fname:
        return jsonify({"status":"error","message":"No filename provided"}),400

    path = os.path.join(UPLOAD_FOLDER, fname)
    if os.path.exists(path):
        os.remove(path)
        if fname in parsed_csv_files:
            del parsed_csv_files[fname]
        return jsonify({"status":"ok","message":f"CSV file '{fname}' deleted from server"}),200
    else:
        return jsonify({"status":"error","message":"File not found on disk"}),404

@app.route('/transactions/assign/<int:trx_index>/<int:cat_id>', methods=['POST'])
def assign_category(trx_index, cat_id):
    if 0 <= trx_index < len(transactions):
        transactions[trx_index]['DetectedCategoryId'] = cat_id
    flash("Transaction category updated.", 'success')
    return redirect(url_for('view_transactions'))

# --------------------- JSON Categories Handling ----------------------

@app.route('/categories/upload_json', methods=['POST'])
def upload_categories_json():
    if 'json_file' not in request.files:
        flash("No JSON file found in request", "danger")
        return redirect(url_for('view_transactions'))

    file = request.files['json_file']
    if file.filename == '':
        flash("No selected JSON file.", "danger")
        return redirect(url_for('view_transactions'))

    filename = secure_filename(file.filename)
    if not filename.lower().endswith('.json'):
        flash("Only .json files allowed", "danger")
        return redirect(url_for('view_transactions'))

    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    save_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(save_path)

    flash(f"JSON file '{filename}' uploaded. You can select it in the .json list overlay.", "success")
    return redirect(url_for('view_transactions'))

@app.route('/categories/select_json', methods=['POST'])
def select_categories_json():
    global categories, groups, lists_data
    global next_category_id, next_group_id, next_list_id
    global active_json_file

    filename = request.form.get('filename','')
    if not filename:
        flash("No JSON filename provided", "danger")
        return redirect(url_for('view_transactions'))

    if active_json_file and active_json_file != filename:
        flash("Cannot select a new JSON file until the current one is deselected.", "danger")
        return redirect(url_for('view_transactions'))

    try:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(filepath):
            flash("File not found on disk.", "danger")
            return redirect(url_for('view_transactions'))

        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        categories = data.get("categories", [])
        groups = data.get("groups", [])
        lists_in = data.get("lists_data", [])
        new_lists = []
        for lst_obj in lists_in:
            tk_arr = lst_obj.get('transaction_keys', [])
            real_ids = []
            for tkey in tk_arr:
                i = find_trx_index_by_key(tkey)
                if i >= 0:
                    real_ids.append(i)
            new_list = {
                'id': lst_obj['id'],
                'name': lst_obj['name'],
                'color': lst_obj['color'],
                'refund_list': lst_obj.get('refund_list', False),
                'transaction_ids': real_ids,
                'list_as_cat': lst_obj.get('list_as_cat', False)
            }
            new_lists.append(new_list)

        lists_data = new_lists

        if categories:
            max_cat_id = max(c['id'] for c in categories if 'id' in c)
            next_category_id = max_cat_id + 1
        else:
            next_category_id = 1

        if groups:
            max_group_id = max(g['id'] for g in groups if 'id' in g)
            next_group_id = max_group_id + 1
        else:
            next_group_id = 1

        if lists_data:
            max_list_id = max(l['id'] for l in lists_data if 'id' in l)
            next_list_id = max_list_id + 1
        else:
            next_list_id = 1

        for trx in transactions:
            cid = trx.get('DetectedCategoryId')
            if not any(c['id'] == cid for c in categories):
                trx['DetectedCategoryId'] = None

        active_json_file = filename
        reclassify_all_transactions_in_memory()
        flash(f"Categories JSON '{filename}' is now active!", "success")

    except Exception as e:
        flash(f"Error selecting JSON: {e}", "danger")

    return redirect(url_for('view_transactions'))

@app.route('/categories/deselect_json', methods=['POST'])
def deselect_categories_json():
    global categories, groups, lists_data
    global active_json_file
    global next_category_id, next_group_id, next_list_id

    categories = []
    groups = []
    lists_data = []
    active_json_file = None
    next_category_id = 1
    next_group_id = 1
    next_list_id = 1

    for trx in transactions:
        trx['DetectedCategoryId'] = None

    flash("JSON file has been deselected. All categories, groups, and lists cleared.", "success")
    return redirect(url_for('view_transactions'))

# --------------------- Categories CRUD & Export/Import ----------------------

@app.route('/categories')
def manage_categories():
    cat_stats_map = {}
    for c in categories:
        cat_stats_map[c['id']] = [0.0, 0]

    # We'll also compute total_exp so categories.html can show the percentage
    total_exp, ref_exp, after_exp = compute_global_expenses()  # <--- new

    for idx, trx in enumerate(transactions):
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            if cid in cat_stats_map:
                cat_stats_map[cid][0] += trx['Betrag']
                cat_stats_map[cid][1] += 1

    for c in categories:
        s, ct = cat_stats_map.get(c['id'], [0.0, 0])
        c['_sum'] = s
        c['_count'] = ct

    group_map = {}
    for g in groups:
        group_map[g['id']] = {
            'group': g,
            'categories': []
        }

    no_group_cats = []
    for c in categories:
        g_id = c.get('group_id')
        if g_id in group_map:
            group_map[g_id]['categories'].append(c)
        else:
            no_group_cats.append(c)

    uncategorized_count = sum(1 for trx in transactions if trx.get('DetectedCategoryId') is None)

    return render_template(
        'categories.html',
        group_map=group_map,
        no_group_cats=no_group_cats,
        uncategorized_count=uncategorized_count,
        groups=groups,
        lists_data=lists_data,
        transactions=transactions,
        total_exp=total_exp   # <-- pass total_exp so we can compute % in categories.html
    )


@app.route('/categories/create', methods=['POST'])
def create_category():
    global next_category_id
    name = request.form.get('category_name','Unnamed').strip()
    color = request.form.get('category_color','#ffffff').strip()

    cat = {
        'id': next_category_id,
        'name': name,
        'color': color,
        'rules': [],
        'group_id': None,
        'show_up_as_group': False,
        '_sum': 0.0,
        '_count': 0
    }
    categories.append(cat)
    next_category_id += 1

    return jsonify({
        "status": "ok",
        "cat_id": cat['id'],
        "cat_name": cat['name'],
        "cat_color": cat['color'],
        "cat_sum": cat['_sum'],
        "cat_count": cat['_count']
    })

@app.route('/categories/view/<int:cat_id>')
def view_category_transactions(cat_id):
    cat_obj = get_category_by_id(cat_id)
    if not cat_obj:
        flash("Category not found", "danger")
        return redirect(url_for('manage_categories'))

    sort_mode = request.args.get('sort','lowest')

    data = []
    for idx, trx in enumerate(transactions):
        if trx.get('DetectedCategoryId') == cat_id:
            data.append((trx, cat_obj['name'], cat_obj['color'], idx))

    if sort_mode == 'lowest':
        data.sort(key=lambda x: x[0].get('Betrag',0.0))
    elif sort_mode == 'highest':
        data.sort(key=lambda x: x[0].get('Betrag',0.0), reverse=True)
    elif sort_mode == 'latest_date':
        def parse_date(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d")
            except:
                return datetime.min
        data.sort(key=lambda x: parse_date(x[0].get('Buchungsdatum','')), reverse=True)
    elif sort_mode == 'oldest_date':
        def parse_date(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d")
            except:
                return datetime.min
        data.sort(key=lambda x: parse_date(x[0].get('Buchungsdatum','')))

    (total_overall,
     refundable_sum,
     after_refund,
     total_excluding_lists,
     total_list_items) = get_trx_amounts_for_category(cat_id)

    return render_template(
        'category_transactions.html',
        category=cat_obj,
        transactions_data=data,
        sort_mode=sort_mode,
        total=total_overall,
        refundable=refundable_sum,
        after_refund=after_refund,
        total_excluding_lists=total_excluding_lists,
        total_list_items=total_list_items,
        categories=categories,
        lists_data=lists_data
    )

@app.route('/ajax/category/rename', methods=['POST'])
def ajax_rename_category():
    cat_id = int(request.form.get('cat_id','0'))
    new_name = request.form.get('new_name','').strip()
    cat = get_category_by_id(cat_id)
    if cat:
        cat['name'] = new_name
        return jsonify({"status":"ok","message":f"Renamed category {cat_id} to '{new_name}'"})
    return jsonify({"status":"error","message":"Category not found"})

@app.route('/ajax/category/toggle_show_group', methods=['POST'])
def ajax_toggle_show_group():
    cat_id = int(request.form.get('cat_id','0'))
    cat = get_category_by_id(cat_id)
    if cat:
        cat['show_up_as_group'] = not cat['show_up_as_group']
        return jsonify({"status":"ok","message":f"Category '{cat['name']}' => show_as_group={cat['show_up_as_group']}"} )
    return jsonify({"status":"error","message":"Category not found"})

@app.route('/ajax/category/update_color', methods=['POST'])
def ajax_update_category_color():
    cat_id = int(request.form.get('cat_id','0'))
    new_color = request.form.get('new_color','#ffffff')
    cat = get_category_by_id(cat_id)
    if cat:
        cat['color'] = new_color
        return jsonify({"status":"ok","message":f"Updated color of category {cat_id} => {new_color}"})
    return jsonify({"status":"error","message":"Category not found"})

@app.route('/ajax/category/assign_group', methods=['POST'])
def ajax_assign_group():
    cat_id = int(request.form.get('cat_id','0'))
    g_val = request.form.get('group_id','')
    cat = get_category_by_id(cat_id)
    if not cat:
        return jsonify({"status":"error","message":"Category not found"})

    if g_val == '':
        cat['group_id'] = None
        return jsonify({"status":"ok","message":f"Category {cat_id} => group_id=None"})
    else:
        g_id = int(g_val)
        group_obj = get_group_by_id(g_id)
        if group_obj:
            cat['group_id'] = g_id
            return jsonify({"status":"ok","message":f"Category {cat_id} assigned to group {g_id}"})
        else:
            return jsonify({"status":"error","message":"Group not found"})

@app.route('/ajax/category/add_rule', methods=['POST'])
def ajax_add_rule():
    cat_id = int(request.form.get('cat_id','0'))
    rule = request.form.get('rule_word','').strip()
    cat = get_category_by_id(cat_id)
    if cat and rule:
        cat['rules'].append(rule)
        reclassify_all_transactions_in_memory()
        return jsonify({
            "status":"ok",
            "message":f"Rule '{rule}' added to category {cat_id}"
        })
    return jsonify({
        "status":"error",
        "message":"Category not found or invalid rule"
    })

@app.route('/ajax/category/remove_rule', methods=['POST'])
def ajax_remove_rule():
    cat_id = int(request.form.get('cat_id','0'))
    rule = request.form.get('rule_word','').strip()
    cat = get_category_by_id(cat_id)
    if cat:
        if rule in cat['rules']:
            cat['rules'].remove(rule)
            reclassify_all_transactions_in_memory()
            return jsonify({
                "status":"ok",
                "message":f"Rule '{rule}' removed from category {cat_id}"
            })
        else:
            return jsonify({
                "status":"error",
                "message":f"Rule '{rule}' not found in category {cat_id}"
            })
    return jsonify({"status":"error","message":"Category not found"})

@app.route('/ajax/category/delete', methods=['POST'])
def ajax_delete_category():
    cat_id = int(request.form.get('cat_id','0'))
    global categories
    categories = [c for c in categories if c['id'] != cat_id]
    for trx in transactions:
        if trx.get('DetectedCategoryId') == cat_id:
            trx['DetectedCategoryId'] = None
    return jsonify({"status":"ok","message":f"Category {cat_id} deleted"})

# ---------------- GROUPS ----------------

@app.route('/groups/create', methods=['POST'])
def create_group():
    global next_group_id
    name = request.form.get('group_name','Unnamed Group').strip()
    color = request.form.get('group_color','#888888')
    g = {
        'id': next_group_id,
        'name': name,
        'color': color
    }
    groups.append(g)
    next_group_id += 1
    flash(f"Group '{name}' created.", "success")
    return redirect(url_for('manage_categories'))

@app.route('/ajax/group/rename', methods=['POST'])
def ajax_rename_group():
    group_id = int(request.form.get('group_id','0'))
    new_name = request.form.get('new_name','').strip()
    grp = get_group_by_id(group_id)
    if grp:
        grp['name'] = new_name
        return jsonify({"status":"ok","message":f"Renamed group {group_id} => '{new_name}'"})
    return jsonify({"status":"error","message":"Group not found"})

@app.route('/ajax/group/update_color', methods=['POST'])
def ajax_update_group_color():
    group_id = int(request.form.get('group_id','0'))
    new_color = request.form.get('new_color','#888888')
    grp = get_group_by_id(group_id)
    if grp:
        grp['color'] = new_color
        return jsonify({"status":"ok","message":f"Updated group {group_id} color => {new_color}"})
    return jsonify({"status":"error","message":"Group not found"})

@app.route('/ajax/group/delete', methods=['POST'])
def ajax_delete_group():
    group_id = int(request.form.get('group_id','0'))
    global groups
    groups = [gg for gg in groups if gg['id'] != group_id]
    for c in categories:
        if c.get('group_id') == group_id:
            c['group_id'] = None
    return jsonify({"status":"ok","message":f"Group {group_id} deleted"})

@app.route('/groups/delete/<int:group_id>', methods=['POST'])
def delete_group(group_id):
    global groups
    groups = [gg for gg in groups if gg['id'] != group_id]
    for c in categories:
        if c.get('group_id') == group_id:
            c['group_id'] = None
    flash("Group deleted.", "success")
    return redirect(url_for('manage_categories'))

@app.route('/groups/view/<int:group_id>')
def view_group_transactions(group_id):
    g = get_group_by_id(group_id)
    if not g:
        flash("Group not found", "danger")
        return redirect(url_for('manage_categories'))
    cat_ids = [c['id'] for c in categories if c.get('group_id') == group_id]
    results = []
    for idx, trx in enumerate(transactions):
        if trx.get('DetectedCategoryId') in cat_ids:
            cat_obj = get_category_by_id(trx['DetectedCategoryId'])
            cat_name = cat_obj['name'] if cat_obj else "Unknown"
            results.append((idx, trx, cat_name))
    return render_template('group_transactions.html', group=g, results=results)

# ---------------- LISTS -----------------

@app.route('/lists')
def manage_lists():
    return render_template('lists.html', lists_data=lists_data, transactions=transactions)

@app.route('/lists/create', methods=['POST'])
def create_list():
    global next_list_id
    name = request.form.get('list_name','Untitled List').strip()
    color = request.form.get('list_color','#0000ff')
    is_refund = bool(request.form.get('refund_list',''))
    list_as_cat = bool(request.form.get('list_as_cat',''))

    new_list = {
        'id': next_list_id,
        'name': name,
        'color': color,
        'refund_list': is_refund,
        'transaction_ids': [],
        'list_as_cat': list_as_cat
    }
    lists_data.append(new_list)
    next_list_id += 1
    flash(f"List '{name}' created.", "success")
    return redirect(url_for('manage_categories'))

@app.route('/lists/add_transaction', methods=['POST'])
def add_trx_to_list():
    list_id = int(request.form.get('list_id','0'))
    trx_index = int(request.form.get('trx_index','-1'))
    lobj = get_list_by_id(list_id)
    if not lobj:
        flash("List not found", "danger")
        return redirect(url_for('manage_categories'))

    if 0 <= trx_index < len(transactions):
        if trx_index not in lobj['transaction_ids']:
            lobj['transaction_ids'].append(trx_index)
            flash(f"Transaction {trx_index} added to list '{lobj['name']}'", "success")
        else:
            flash(f"Transaction {trx_index} already in list '{lobj['name']}'", "info")
    else:
        flash("Invalid transaction index.", "danger")

    return redirect(url_for('manage_categories'))

@app.route('/lists/remove_transaction', methods=['POST'])
def remove_trx_from_list():
    list_id = int(request.form.get('list_id','0'))
    trx_index = int(request.form.get('trx_index','-1'))
    lobj = get_list_by_id(list_id)
    if not lobj:
        flash("List not found", "danger")
        return redirect(url_for('manage_categories'))

    if trx_index in lobj['transaction_ids']:
        lobj['transaction_ids'].remove(trx_index)
        flash(f"Transaction {trx_index} removed from list '{lobj['name']}'", "success")
    else:
        flash(f"Transaction {trx_index} not in list '{lobj['name']}'", "warning")

    return redirect(url_for('manage_categories'))

@app.route('/lists/rename/<int:list_id>', methods=['POST'])
def rename_list(list_id):
    new_name = request.form.get('new_name','').strip()
    lobj = get_list_by_id(list_id)
    if lobj:
        lobj['name'] = new_name
        flash(f"List renamed to '{new_name}'", "success")
    return "OK"

@app.route('/lists/toggle_refund/<int:list_id>', methods=['POST'])
def toggle_refund_list(list_id):
    lobj = get_list_by_id(list_id)
    if lobj:
        lobj['refund_list'] = not lobj['refund_list']
        flash(f"List '{lobj['name']}' refund toggled -> {lobj['refund_list']}", "success")
    return "OK"

@app.route('/lists/delete/<int:list_id>', methods=['POST'])
def delete_list(list_id):
    global lists_data
    lists_data = [l for l in lists_data if l['id'] != list_id]
    flash("List deleted", "success")
    return "OK"

@app.route('/lists/view/<int:list_id>')
def view_list_transactions(list_id):
    lobj = get_list_by_id(list_id)
    if not lobj:
        flash("List not found", "danger")
        return redirect(url_for('manage_categories'))
    items = []
    total_amt = 0.0
    for idx in lobj['transaction_ids']:
        if 0 <= idx < len(transactions):
            trx = transactions[idx]
            amt = trx.get('Betrag',0.0)
            total_amt += amt
            is_refund = lobj['refund_list']
            items.append((idx, trx, is_refund))

    return render_template('list_transactions.html',
                           the_list=lobj,
                           items=items,
                           total=total_amt)

# ------------------ Import / Export (Existing) ------------------

@app.route('/categories/export', methods=['GET'])
def export_categories():
    def lists_to_json():
        result = []
        for lobj in lists_data:
            keys_arr = []
            for idx in lobj['transaction_ids']:
                if 0 <= idx < len(transactions):
                    trx = transactions[idx]
                    keys_arr.append(build_transaction_key(trx))
            result.append({
                'id': lobj['id'],
                'name': lobj['name'],
                'color': lobj['color'],
                'refund_list': lobj['refund_list'],
                'list_as_cat': lobj.get('list_as_cat', False),
                'transaction_keys': keys_arr
            })
        return result

    data = {
        "categories": categories,
        "groups": groups,
        "lists_data": lists_to_json()
    }
    resp = make_response(json.dumps(data, indent=2))
    resp.headers['Content-Type'] = 'application/json'
    resp.headers['Content-Disposition'] = 'attachment; filename=categories.json'
    return resp

@app.route('/categories/import', methods=['POST'])
def import_categories():
    if 'categories_json' not in request.files:
        flash("No JSON file in request", "danger")
        return redirect(url_for('manage_categories'))
    file = request.files['categories_json']
    if file.filename == '':
        flash("No selected JSON file.", "danger")
        return redirect(url_for('manage_categories'))

    try:
        content = file.read().decode('utf-8', errors='replace')
        data = json.loads(content)

        global categories, groups, lists_data
        global next_category_id, next_group_id, next_list_id

        categories = data.get("categories", [])
        groups = data.get("groups", [])
        lists_in = data.get("lists_data", [])
        new_lists = []
        for lst_obj in lists_in:
            tk_arr = lst_obj.get('transaction_keys', [])
            real_ids = []
            for tkey in tk_arr:
                i = find_trx_index_by_key(tkey)
                if i >= 0:
                    real_ids.append(i)
            new_list = {
                'id': lst_obj['id'],
                'name': lst_obj['name'],
                'color': lst_obj['color'],
                'refund_list': lst_obj.get('refund_list', False),
                'transaction_ids': real_ids,
                'list_as_cat': lst_obj.get('list_as_cat', False)
            }
            new_lists.append(new_list)

        lists_data = new_lists

        if categories:
            max_cat_id = max(c['id'] for c in categories if 'id' in c)
            next_category_id = max_cat_id + 1
        else:
            next_category_id = 1

        if groups:
            max_group_id = max(g['id'] for g in groups if 'id' in g)
            next_group_id = max_group_id + 1
        else:
            next_group_id = 1

        if lists_data:
            max_list_id = max(l['id'] for l in lists_data if 'id' in l)
            next_list_id = max_list_id + 1
        else:
            next_list_id = 1

        for trx in transactions:
            cid = trx.get('DetectedCategoryId')
            if not any(c['id'] == cid for c in categories):
                trx['DetectedCategoryId'] = None

        flash("Imported categories, groups, lists from JSON (with transaction_keys).", "success")
        reclassify_all_transactions_in_memory()

    except Exception as e:
        logger.error(f"Error importing JSON: {e}")
        flash(f"Error importing JSON: {e}", "danger")

    return redirect(url_for('manage_categories'))

# ------------------ STATS ------------------
@app.route('/stats')
def stats():
    sort_mode = request.args.get('sort','lowest')
    total_exp, ref_exp, after_exp = compute_global_expenses()
    total_inc = compute_global_income()

    cat_sums = {}
    cat_counts = {}
    cat_sums[None] = 0.0
    cat_counts[None] = 0

    for c in categories:
        cat_sums[c['id']] = 0.0
        cat_counts[c['id']] = 0

    for idx, trx in enumerate(transactions):
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            cat_sums.setdefault(cid,0.0)
            cat_counts.setdefault(cid,0)
            cat_sums[cid] += trx['Betrag']
            cat_counts[cid] += 1

    data_list = []
    # We'll also need total_exp for the percentage logic:
    #   percentage = (cat_sums / total_exp)*100   if total_exp < 0
    for c in categories:
        cid = c['id']
        amt = cat_sums[cid]
        ct = cat_counts[cid]
        pct = 0.0
        if total_exp < 0.0:  # total_exp is negative
            pct = (amt / total_exp) * 100.0  # negative / negative => positive %
        data_list.append({
            "cat_id": cid,
            "name": c['name'],
            "color": c['color'],
            "amount": amt,
            "count": ct,
            "pct": pct
        })

    if cat_counts[None] > 0:
        amt_unassigned = cat_sums[None]
        pct_unassigned = 0.0
        if total_exp < 0.0:
            pct_unassigned = (amt_unassigned / total_exp) * 100.0
        data_list.append({
            "cat_id": None,
            "name": "UNK",
            "color": "#dddddd",
            "amount": amt_unassigned,
            "count": cat_counts[None],
            "pct": pct_unassigned
        })

    # Sorting
    if sort_mode == 'highest':
        data_list.sort(key=lambda x: x['amount'], reverse=True)
    elif sort_mode == 'lowest':
        data_list.sort(key=lambda x: x['amount'])
    elif sort_mode == 'most_transactions':
        data_list.sort(key=lambda x: x['count'], reverse=True)
    elif sort_mode == 'least_transactions':
        data_list.sort(key=lambda x: x['count'])

    # Summaries for groups
    group_sums = {}
    group_counts = {}
    for g in groups:
        group_sums[g['id']] = 0.0
        group_counts[g['id']] = 0

    for c in categories:
        amt = cat_sums[c['id']]
        ct = cat_counts[c['id']]
        g_id = c.get('group_id')
        group_sums.setdefault(g_id, 0.0)
        group_counts.setdefault(g_id, 0)
        group_sums[g_id] += amt
        group_counts[g_id] += ct

    group_data_list = []
    for g in groups:
        amt_g = group_sums[g['id']]
        ct_g = group_counts[g['id']]
        pct_g = 0.0
        if total_exp < 0.0:
            pct_g = (amt_g / total_exp) * 100.0
        group_data_list.append({
            "group_id": g['id'],
            "name": g['name'],
            "color": g.get('color','#888888'),
            "amount": amt_g,
            "count": ct_g,
            "pct": pct_g
        })

    # categories that show_up_as_group
    for c in categories:
        if c.get('show_up_as_group'):
            amt_c = cat_sums[c['id']]
            ct_c = cat_counts[c['id']]
            pct_c = 0.0
            if total_exp < 0.0:
                pct_c = (amt_c / total_exp) * 100.0
            group_data_list.append({
                "group_id": -c['id'],
                "name": f"[CatAsGroup] {c['name']}",
                "color": c['color'],
                "amount": amt_c,
                "count": ct_c,
                "pct": pct_c
            })

    return render_template('stats.html',
                           category_stats=data_list,
                           group_stats=group_data_list,
                           sort_mode=sort_mode,
                           total_exp=total_exp,
                           ref_exp=ref_exp,
                           after_exp=after_exp,
                           total_inc=total_inc,
                           lists_data=lists_data)

@app.route('/stats/export_interactive')
def export_interactive_charts():
    cat_sums = {}
    cat_sums[None] = 0.0
    for c in categories:
        cat_sums[c['id']] = 0.0

    for trx in transactions:
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            cat_sums.setdefault(cid, 0.0)
            cat_sums[cid] += trx['Betrag']

    data = []
    for c in categories:
        data.append({"Category": c['name'], "Amount": cat_sums[c['id']]})

    df = pd.DataFrame(data)
    fig = px.bar(df, x="Category", y="Amount", title="Interactive Expense Chart")
    html_str = pio.to_html(fig, full_html=True)
    resp = make_response(html_str)
    resp.headers['Content-Type'] = 'text/html'
    return resp

@app.route('/stats/bar_chart.png')
def bar_chart_png():
    cat_sums = {}
    cat_sums[None] = 0.0
    for c in categories:
        cat_sums[c['id']] = 0.0

    for trx in transactions:
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            cat_sums.setdefault(cid, 0.0)
            cat_sums[cid] += trx['Betrag']

    cat_names = []
    sums = []
    colors = []

    for c in categories:
        cat_names.append(c['name'])
        sums.append(cat_sums[c['id']])
        colors.append(c['color'])

    if cat_sums[None] != 0.0:
        cat_names.append("UNK")
        sums.append(cat_sums[None])
        colors.append("#dddddd")

    if len(cat_names) == 0:
        cat_names = ["No data"]
        sums = [0]
        colors = ["#999999"]

    fig, ax = plt.subplots(figsize=(6,4))
    bars = ax.bar(cat_names, sums, color=colors)

    ax.set_title("Total Negative Amount by Category (PNG)")
    ax.set_xlabel("Category")
    ax.set_ylabel("Amount")
    plt.xticks(rotation=45, ha='right')

    # Label each bar with its value
    for rect, val in zip(bars, sums):
        height = rect.get_height()
        if val >= 0:
            ax.text(rect.get_x() + rect.get_width()/2, height, f"{val:.2f}",
                    ha='center', va='bottom', fontsize=8)
        else:
            ax.text(rect.get_x() + rect.get_width()/2, height, f"{val:.2f}",
                    ha='center', va='top', fontsize=8)

    total_val = sum(sums)
    ax.text(0.02, 0.98, f"Total: {total_val:.2f}",
            transform=ax.transAxes, ha='left', va='top', fontsize=10, color='#000000')

    plt.tight_layout()

    # Instead of returning the in-memory stream directly,
    # write it to a temp file:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        plt.savefig(tmp.name, format='png')
        tmp_path = tmp.name
    plt.close(fig)

    # Now serve that file via send_file:
    return send_file(tmp_path, mimetype='image/png')

# ...
@app.route('/stats/group_bar_chart.png')
def group_bar_chart_png():
    cat_sums = {}
    for c in categories:
        cat_sums[c['id']] = 0.0

    for trx in transactions:
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            cat_sums.setdefault(cid, 0.0)
            cat_sums[cid] += trx['Betrag']

    group_sums = {}
    for g in groups:
        group_sums[g['id']] = 0.0

    for c in categories:
        g_id = c.get('group_id')
        group_sums.setdefault(g_id,0.0)
        group_sums[g_id] += cat_sums[c['id']]

    show_as_group = []
    for c in categories:
        if c.get('show_up_as_group'):
            show_as_group.append(("[CatAsGroup] "+c['name'], cat_sums[c['id']], c['color']))

    group_names = []
    group_vals = []
    color_list = []

    for g in groups:
        group_names.append(g['name'])
        group_vals.append(group_sums[g['id']])
        color_list.append(g['color'])

    for (lbl, val, col) in show_as_group:
        group_names.append(lbl)
        group_vals.append(val)
        color_list.append(col)

    if len(group_names) == 0:
        group_names = ["No data"]
        group_vals = [0]
        color_list = ["#999999"]

    fig, ax = plt.subplots(figsize=(6,4))
    bars = ax.bar(group_names, group_vals, color=color_list)

    ax.set_title("Total Negative Amount by Group (PNG)")
    ax.set_xlabel("Group")
    ax.set_ylabel("Amount")
    plt.xticks(rotation=45, ha='right')

    for rect, val in zip(bars, group_vals):
        height = rect.get_height()
        if val >= 0:
            ax.text(rect.get_x() + rect.get_width()/2, height, f"{val:.2f}",
                    ha='center', va='bottom', fontsize=8)
        else:
            ax.text(rect.get_x() + rect.get_width()/2, height, f"{val:.2f}",
                    ha='center', va='top', fontsize=8)

    total_val = sum(group_vals)
    ax.text(0.02, 0.98, f"Total: {total_val:.2f}",
            transform=ax.transAxes, ha='left', va='top', fontsize=10, color='#000000')

    plt.tight_layout()

    # Save to temp file:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        plt.savefig(tmp.name, format='png')
        tmp_path = tmp.name
    plt.close(fig)

    return send_file(tmp_path, mimetype='image/png')

@app.route('/stats/download_bar_chart')
def download_bar_chart():
    cat_sums = {}
    cat_sums[None] = 0.0
    for c in categories:
        cat_sums[c['id']] = 0.0

    for trx in transactions:
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            cat_sums[cid] += trx['Betrag']

    cat_names = []
    sums = []
    colors = []
    for c in categories:
        cat_names.append(c['name'])
        sums.append(cat_sums[c['id']])
        colors.append(c['color'])

    if cat_sums[None] != 0.0:
        cat_names.append("UNK")
        sums.append(cat_sums[None])
        colors.append("#dddddd")

    if len(cat_names) == 0:
        cat_names = ["No data"]
        sums = [0]
        colors = ["#999999"]

    fig, ax = plt.subplots(figsize=(6,4))
    ax.bar(cat_names, sums, color=colors)
    ax.set_title("Total Negative Amount by Category (PNG)")
    ax.set_xlabel("Category")
    ax.set_ylabel("Amount")
    plt.xticks(rotation=45, ha='right')
    plt.tight_layout()

    out = io.BytesIO()
    plt.savefig(out, format='png')
    plt.close(fig)
    out.seek(0)

    return send_file(
        out,
        mimetype='image/png',
        as_attachment=True,
        download_name='category_chart.png'
    )

@app.route('/stats/download_group_chart')
def download_group_chart():
    cat_sums = {}
    for c in categories:
        cat_sums[c['id']] = 0.0

    for trx in transactions:
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            cat_sums.setdefault(cid,0.0)
            cat_sums[cid] += trx['Betrag']

    group_sums = {}
    for g in groups:
        group_sums[g['id']] = 0.0

    for c in categories:
        g_id = c.get('group_id')
        group_sums.setdefault(g_id, 0.0)
        group_sums[g_id] += cat_sums[c['id']]

    show_as_group = []
    for c in categories:
        if c.get('show_up_as_group'):
            show_as_group.append(("[CatAsGroup] "+c['name'], cat_sums[c['id']], c['color']))

    group_names = []
    group_vals = []
    color_list = []
    for g in groups:
        group_names.append(g['name'])
        group_vals.append(group_sums[g['id']])
        color_list.append(g['color'])

    for (lbl, val, col) in show_as_group:
        group_names.append(lbl)
        group_vals.append(val)
        color_list.append(col)

    if len(group_names) == 0:
        group_names = ["No data"]
        group_vals = [0]
        color_list = ["#999999"]

    fig, ax = plt.subplots(figsize=(6,4))
    ax.bar(group_names, group_vals, color=color_list)
    ax.set_title("Total Negative Amount by Group (PNG)")
    ax.set_xlabel("Group")
    ax.set_ylabel("Amount")
    plt.xticks(rotation=45, ha='right')
    plt.tight_layout()

    out = io.BytesIO()
    plt.savefig(out, format='png')
    plt.close(fig)
    out.seek(0)

    return send_file(
        out,
        mimetype='image/png',
        as_attachment=True,
        download_name='group_chart.png'
    )

@app.route('/categories/view/unassigned')
def view_unassigned_transactions():
    data = []
    for idx, trx in enumerate(transactions):
        if trx.get('DetectedCategoryId') is None:
            data.append((trx, "UNK", "#dddddd", idx))

    total = 0.0
    refundable = 0.0
    for idx, tup in enumerate(data):
        real_trx = tup[0]
        real_idx = tup[3]
        if is_expense(real_trx):
            amt = real_trx.get('Betrag',0.0)
            total += amt
            if compute_refund_status(real_idx):
                refundable += amt
    after_refund = total - refundable

    return render_template(
        'unassigned_transactions.html',
        transactions_data=data,
        categories=categories,
        lists_data=lists_data,
        total=total,
        refundable=refundable,
        after_refund=after_refund
    )

##################################
# AJAX endpoints for adding/removing from a list
##################################

@app.route('/ajax/add_trx_to_list', methods=['POST'])
def ajax_add_trx_to_list():
    data = request.get_json()
    if not data:
        return jsonify({"status":"error","message":"No JSON data"}), 400
    
    trx_index = data.get("trx_index")
    list_id = data.get("list_id")
    if trx_index is None or list_id is None:
        return jsonify({"status":"error","message":"Missing trx_index or list_id"}), 400
    
    if not (0 <= trx_index < len(transactions)):
        return jsonify({"status":"error","message":"Invalid trx_index"}), 400
    
    lobj = get_list_by_id(list_id)
    if not lobj:
        return jsonify({"status":"error","message":"List not found"}), 404

    if trx_index not in lobj['transaction_ids']:
        lobj['transaction_ids'].append(trx_index)
        logger.debug(f"Transaction {trx_index} added to list {lobj['name']}")
        return jsonify({
            "status":"ok",
            "message":f"Transaction {trx_index} added to list '{lobj['name']}'",
            "added":True
        })
    else:
        return jsonify({
            "status":"ok",
            "message":f"Transaction {trx_index} already in list '{lobj['name']}'",
            "added":False
        })

@app.route('/ajax/unassign_category', methods=['POST'])
def ajax_unassign_category():
    data = request.get_json()
    if not data:
        return jsonify({"status":"error","message":"No JSON data provided"}), 400

    trx_index = data.get("trx_index")
    if trx_index is None:
        return jsonify({"status":"error","message":"Missing 'trx_index'"}), 400

    if not (0 <= trx_index < len(transactions)):
        return jsonify({"status":"error","message":"Invalid transaction index"}), 400

    transactions[trx_index]['DetectedCategoryId'] = None
    return jsonify({
        "status":"ok",
        "message":f"Transaction {trx_index} unassigned from any category"
    }), 200

@app.route('/ajax/assign_category', methods=['POST'])
def ajax_assign_category():
    data = request.get_json()
    trx_index = data.get('trx_index')
    cat_id = data.get('cat_id')
    
    if trx_index is None or cat_id is None:
        return jsonify({'status': 'error', 'message': 'Missing trx_index or cat_id'}), 400
    
    if not (0 <= trx_index < len(transactions)):
        return jsonify({'status': 'error', 'message': 'Invalid transaction index'}), 400
    
    cat = get_category_by_id(cat_id)
    if not cat:
        return jsonify({'status': 'error', 'message': 'Category not found'}), 404
    
    transactions[trx_index]['DetectedCategoryId'] = cat_id
    return jsonify({
        'status': 'ok',
        'message': f"Transaction {trx_index} assigned to category '{cat['name']}'"
    }), 200

@app.route('/ajax/remove_trx_from_list', methods=['POST'])
def ajax_remove_trx_from_list():
    data = request.get_json()
    if not data:
        return jsonify({"status":"error","message":"No JSON data"}), 400

    trx_index = data.get("trx_index")
    list_id = data.get("list_id")
    if trx_index is None or list_id is None:
        return jsonify({"status":"error","message":"Missing trx_index or list_id"}), 400

    if not (0 <= trx_index < len(transactions)):
        return jsonify({"status":"error","message":"Invalid trx_index"}), 400

    lobj = get_list_by_id(list_id)
    if not lobj:
        return jsonify({"status":"error","message":"List not found"}), 404

    if trx_index in lobj['transaction_ids']:
        lobj['transaction_ids'].remove(trx_index)
        logger.debug(f"Transaction {trx_index} removed from list {lobj['name']}")
        return jsonify({
            "status":"ok",
            "message":f"Transaction {trx_index} removed from list '{lobj['name']}'",
            "removed":True
        })
    else:
        return jsonify({
            "status":"ok",
            "message":f"Transaction {trx_index} not in list '{lobj['name']}'",
            "removed":False
        })

@app.route('/ajax/search_transactions', methods=['GET'])
def ajax_search_transactions():
    query = request.args.get('q','').lower()
    sort_mode = request.args.get('sort','lowest')

    filtered_data = []
    for idx, trx in enumerate(transactions):
        full_text = (trx.get('Buchungstext','') + " " + trx.get('Umsatztext','')).lower()
        if query in full_text:
            cat = get_category_by_id(trx.get('DetectedCategoryId'))
            cat_name = cat['name'] if cat else "UNK"
            cat_color = cat['color'] if cat else "#dddddd"
            filtered_data.append({
                "idx": idx,
                "buchungstext": trx.get('Buchungstext',''),
                "umsatztext": trx.get('Umsatztext',''),
                "partner": trx.get('Name des Partners',''),
                "betrag": trx.get('Betrag',0.0),
                "buchungsdatum": trx.get('Buchungsdatum',''),
                "cat_id": cat['id'] if cat else None,
                "cat_name": cat_name,
                "cat_color": cat_color
            })

    if sort_mode == 'lowest':
        filtered_data.sort(key=lambda x: x['betrag'])
    elif sort_mode == 'highest':
        filtered_data.sort(key=lambda x: x['betrag'], reverse=True)
    elif sort_mode == 'latest_date':
        def parse_date(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d")
            except:
                return datetime.min
        filtered_data.sort(key=lambda x: parse_date(x['buchungsdatum']), reverse=True)
    elif sort_mode == 'oldest_date':
        def parse_date(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d")
            except:
                return datetime.min
        filtered_data.sort(key=lambda x: parse_date(x['buchungsdatum']))

    return jsonify({"status":"ok","transactions": filtered_data})

if __name__ == '__main__':
    logger.debug("Starting Flask app (debug=True) on port 4444.")
    app.run(debug=True, port=4444)

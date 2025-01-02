#app.py 

import os
import io
import csv
import json
import logging
from datetime import datetime

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_file,
    flash,
    make_response,
    jsonify
)
from werkzeug.utils import secure_filename

# For static PNG charts
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# For optional pandas/plotly
import pandas as pd
import plotly.express as px
import plotly.io as pio

# ------------------ Setup Logging ------------------
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# ------------------ Flask Setup --------------------
app = Flask(__name__)
app.secret_key = 'SOME_LONG_SECRET_KEY'

# ------------------ In-Memory Data -----------------
ALLOWED_EXTENSIONS = {'csv'}

transactions = []  # Each is { <CSV fields>, 'DetectedCategoryId': int or None }
categories = []    # { 'id', 'name', 'color', 'rules': [...], 'group_id': int|None, 'show_up_as_group': bool }
groups = []        # { 'id', 'name', 'color' }
lists_data = []    # { 'id', 'name', 'color', 'refund_list': bool, 'transaction_ids': [...] }

next_category_id = 1
next_group_id = 1
next_list_id = 1

# ---------------------------------------------------
#                  Helper Functions
# ---------------------------------------------------
def allowed_file(filename):
    logger.debug(f"Checking file extension for: {filename}")
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_csv_and_store(file_stream):
    logger.debug("Parsing CSV file stream...")
    raw_data = file_stream.read()

    # Attempt decode as UTF-8, else fallback to latin-1
    try:
        content_decoded = raw_data.decode('utf-8')
        logger.debug("Decoded as UTF-8.")
    except UnicodeDecodeError as e:
        logger.warning(f"Failed UTF-8 decoding ({e}). Fallback to latin-1.")
        content_decoded = raw_data.decode('latin-1', errors='replace')

    reader = csv.reader(io.StringIO(content_decoded), delimiter=';')
    headers = None

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

        # Auto-classify
        row_data['DetectedCategoryId'] = classify_transaction(row_data)
        transactions.append(row_data)
        logger.debug(f"Stored transaction row {idx}: {row_data}")

def classify_transaction(trx):
    """
    Return the ID of the first-matching category (by 'rules'), else None
    """
    text = (
        (trx.get('Buchungstext','') + ' ' +
         trx.get('Umsatztext','') + ' ' +
         trx.get('Name des Partners','') + ' ' +
         trx.get('Verwendungszweck','')).lower()
    )
    for cat in categories:
        for rule in cat['rules']:
            if rule.lower() in text:
                logger.debug(f"Transaction matched category {cat['name']} by rule {rule}")
                return cat['id']
    return None

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
    # Negative => expense
    amt = trx.get('Betrag', 0.0)
    return (amt < 0)

def compute_refund_status(trx_index):
    """
    Return True if transaction index is in any list with 'refund_list'=True
    """
    for lst in lists_data:
        if lst['refund_list'] and (trx_index in lst['transaction_ids']):
            return True
    return False

def get_trx_amounts_for_category(cat_id):
    """
    For a given category, sum the negative amounts (expense),
    also track how much is 'refundable'.
    """
    total = 0.0
    refundable = 0.0
    for idx, trx in enumerate(transactions):
        if trx.get('DetectedCategoryId') == cat_id and is_expense(trx):
            amt = trx['Betrag']
            total += amt
            if compute_refund_status(idx):
                refundable += amt
    return (total, refundable, total - refundable)

def compute_global_expenses():
    """
    Return (total_expenses, refundable_part, after_refund)
    counting only negative amounts.
    """
    total = 0.0
    refundable = 0.0
    for idx, trx in enumerate(transactions):
        if is_expense(trx):
            amt = trx.get('Betrag', 0.0)
            total += amt
            if compute_refund_status(idx):
                refundable += amt
    return (total, refundable, total - refundable)

def compute_global_income():
    """
    Return sum of all positive amounts.
    """
    total_inc = 0.0
    for trx in transactions:
        if trx.get('Betrag',0.0) >= 0:
            total_inc += trx['Betrag']
    return total_inc

@app.route('/ajax/assign_category', methods=['POST'])
def ajax_assign_category():
    """
    AJAX endpoint to reassign a single transaction to a new category.
    Expects JSON input: { "trx_index": <int>, "cat_id": <int> }
    Returns JSON: { "status": "ok"|"error", "message": "..." }
    """
    data = request.get_json()
    if not data:
        return jsonify({"status":"error","message":"No JSON data provided"}), 400
    
    trx_index = data.get("trx_index")
    cat_id = data.get("cat_id")
    logger.debug(f"AJAX assign_category: trx_index={trx_index}, cat_id={cat_id}")

    if trx_index is None or cat_id is None:
        return jsonify({"status":"error","message":"Missing 'trx_index' or 'cat_id'"}), 400
    
    if not (0 <= trx_index < len(transactions)):
        return jsonify({"status":"error","message":"Invalid transaction index"}), 400

    # Update
    transactions[trx_index]['DetectedCategoryId'] = cat_id
    
    # Optionally re-run classification or do other checks...
    # transactions[trx_index]['DetectedCategoryId'] = classify_transaction(transactions[trx_index])
    
    return jsonify({"status":"ok","message":"Transaction category updated"}), 200

# ---------------------------------------------------
#                       Routes
# ---------------------------------------------------

@app.route('/')
def index():
    logger.debug("Serving home page.")
    return render_template('index.html')

# ----------------- UPLOAD CSV -------------------
@app.route('/upload', methods=['GET','POST'])
def upload_files():
    logger.debug("Serving upload page.")
    if request.method == 'POST':
        files = request.files.getlist('files[]')
        if not files:
            flash("No files found in request", "danger")
            return redirect(request.url)
        for file in files:
            if file.filename and allowed_file(file.filename):
                logger.debug(f"Processing file: {file.filename}")
                parse_csv_and_store(file)
                flash(f"File {file.filename} uploaded & parsed.", "success")
            else:
                flash(f"File {file.filename} not allowed or empty.", "danger")
        return redirect(url_for('upload_files'))
    return render_template('upload.html')

# ----------------- TRANSACTIONS ------------------
@app.route('/transactions')
def view_transactions():
    logger.debug("Serving the transactions page.")
    sort_mode = request.args.get('sort','lowest')

    data = []
    for idx, trx in enumerate(transactions):
        cat = get_category_by_id(trx.get('DetectedCategoryId'))
        cat_name = cat['name'] if cat else "UNK"
        cat_color = cat['color'] if cat else "#dddddd"
        data.append((trx, cat_name, cat_color, idx))

    # Sort logic
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

    # ---------------- NEW: Pass lists_data below ----------------
    return render_template(
        'transactions.html',
        transactions_data=data,
        categories=categories,
        lists_data=lists_data,        # <-- THIS IS THE FIX
        sort_mode=sort_mode,
        total=total_exp,
        refundable=ref_exp,
        after_refund=after_exp
    )


@app.route('/transactions/classify_all', methods=['POST'])
def classify_all_transactions():
    logger.debug("Re-classifying all transactions with current rules.")
    for trx in transactions:
        trx['DetectedCategoryId'] = classify_transaction(trx)
    flash("All transactions re-classified according to updated rules.", 'success')
    return redirect(url_for('view_transactions'))

@app.route('/transactions/assign/<int:trx_index>/<int:cat_id>', methods=['POST'])
def assign_category(trx_index, cat_id):
    logger.debug(f"Assigning category {cat_id} to transaction index {trx_index}.")
    if 0 <= trx_index < len(transactions):
        transactions[trx_index]['DetectedCategoryId'] = cat_id
    flash("Transaction category updated.", 'success')
    return redirect(url_for('view_transactions'))

# ----------------- CATEGORIES & GROUPS ------------------
@app.route('/categories')
def manage_categories():
    logger.debug("Serving categories management page.")
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

    # Count how many are totally uncategorized
    uncategorized_count = 0
    for trx in transactions:
        if trx.get('DetectedCategoryId') is None:
            uncategorized_count += 1

    # IMPORTANT: pass groups=groups here
    return render_template(
        'categories.html',
        group_map=group_map,
        no_group_cats=no_group_cats,
        uncategorized_count=uncategorized_count,
        groups=groups
    )

@app.route('/categories/create', methods=['POST'])
def create_category():
    logger.debug("Creating a new category via form.")
    global next_category_id
    name = request.form.get('category_name','Unnamed').strip()
    color = request.form.get('category_color','#ffffff').strip()

    cat = {
        'id': next_category_id,
        'name': name,
        'color': color,
        'rules': [],
        'group_id': None,
        'show_up_as_group': False
    }
    categories.append(cat)
    next_category_id += 1
    flash(f"Category '{name}' created!", 'success')
    return redirect(url_for('manage_categories'))

# The route that your code calls: "view_category_transactions"
@app.route('/categories/view/<int:cat_id>')
def view_category_transactions(cat_id):
    """
    Enhanced version that looks/behaves like transactions.html:
    - Sorting by ?sort=lowest|highest|latest_date|oldest_date
    - Summaries of total expenses, etc.
    - Same table layout & "Assign" logic
    """
    logger.debug(f"Viewing category transactions: cat_id={cat_id}")
    cat_obj = get_category_by_id(cat_id)
    if not cat_obj:
        flash("Category not found", "danger")
        return redirect(url_for('manage_categories'))

    sort_mode = request.args.get('sort','lowest')

    # Gather matching transactions with same data structure as transactions.html
    data = []
    for idx, trx in enumerate(transactions):
        if trx.get('DetectedCategoryId') == cat_id:
            cat_name = cat_obj['name'] if cat_obj else "UNK"
            cat_color = cat_obj['color'] if cat_obj else "#dddddd"
            data.append((trx, cat_name, cat_color, idx))

    # Sorting logic
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

    # Compute totals for just this category
    total, refundable, after_refund = get_trx_amounts_for_category(cat_id)

    return render_template(
        'category_transactions.html',
        category=cat_obj,
        transactions_data=data,  # rename for clarity
        sort_mode=sort_mode,
        total=total,
        refundable=refundable,
        after_refund=after_refund,
        # Also pass the list of categories for possible reassign
        categories=categories,
        lists_data=lists_data
    )

# -------------- AJAX endpoints for categories --------------
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
        return jsonify({"status":"ok","message":f"Category '{cat['name']}' => show_as_group={cat['show_up_as_group']}"})
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
        return jsonify({"status":"ok","message":f"Rule '{rule}' added to category {cat_id}"})
    return jsonify({"status":"error","message":"Category not found or invalid rule"})

@app.route('/ajax/category/remove_rule', methods=['POST'])
def ajax_remove_rule():
    cat_id = int(request.form.get('cat_id','0'))
    rule = request.form.get('rule_word','').strip()
    cat = get_category_by_id(cat_id)
    if cat:
        cat['rules'] = [r for r in cat['rules'] if r != rule]
        return jsonify({"status":"ok","message":f"Rule '{rule}' removed from category {cat_id}"})
    return jsonify({"status":"error","message":"Category not found"})

@app.route('/ajax/category/delete', methods=['POST'])
def ajax_delete_category():
    cat_id = int(request.form.get('cat_id','0'))
    global categories
    categories = [c for c in categories if c['id'] != cat_id]
    # remove from transactions
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
    new_list = {
        'id': next_list_id,
        'name': name,
        'color': color,
        'refund_list': is_refund,
        'transaction_ids': []
    }
    lists_data.append(new_list)
    next_list_id += 1
    flash(f"List '{name}' created.", "success")
    return redirect(url_for('manage_lists'))

@app.route('/lists/add_transaction', methods=['POST'])
def add_trx_to_list():
    list_id = int(request.form.get('list_id','0'))
    trx_index = int(request.form.get('trx_index','-1'))
    lobj = get_list_by_id(list_id)
    if not lobj:
        flash("List not found", "danger")
        return redirect(url_for('manage_lists'))

    if 0 <= trx_index < len(transactions):
        if trx_index not in lobj['transaction_ids']:
            lobj['transaction_ids'].append(trx_index)
            flash(f"Transaction {trx_index} added to list '{lobj['name']}'", "success")
        else:
            flash(f"Transaction {trx_index} already in list '{lobj['name']}'", "info")
    else:
        flash("Invalid transaction index.", "danger")

    return redirect(url_for('manage_lists'))

@app.route('/lists/remove_transaction', methods=['POST'])
def remove_trx_from_list():
    list_id = int(request.form.get('list_id','0'))
    trx_index = int(request.form.get('trx_index','-1'))
    lobj = get_list_by_id(list_id)
    if not lobj:
        flash("List not found", "danger")
        return redirect(url_for('manage_lists'))

    if trx_index in lobj['transaction_ids']:
        lobj['transaction_ids'].remove(trx_index)
        flash(f"Transaction {trx_index} removed from list '{lobj['name']}'", "success")
    else:
        flash(f"Transaction {trx_index} not in list '{lobj['name']}'", "warning")

    return redirect(url_for('manage_lists'))

@app.route('/lists/rename/<int:list_id>', methods=['POST'])
def rename_list(list_id):
    new_name = request.form.get('new_name','').strip()
    lobj = get_list_by_id(list_id)
    if lobj:
        lobj['name'] = new_name
        flash(f"List renamed to '{new_name}'", "success")
    return redirect(url_for('manage_lists'))

@app.route('/lists/toggle_refund/<int:list_id>', methods=['POST'])
def toggle_refund_list(list_id):
    lobj = get_list_by_id(list_id)
    if lobj:
        lobj['refund_list'] = not lobj['refund_list']
        flash(f"List '{lobj['name']}' refund toggled -> {lobj['refund_list']}", "success")
    return redirect(url_for('manage_lists'))

@app.route('/lists/delete/<int:list_id>', methods=['POST'])
def delete_list(list_id):
    global lists_data
    lists_data = [l for l in lists_data if l['id'] != list_id]
    flash("List deleted", "success")
    return redirect(url_for('manage_lists'))

@app.route('/lists/view/<int:list_id>')
def view_list_transactions(list_id):
    lobj = get_list_by_id(list_id)
    if not lobj:
        flash("List not found", "danger")
        return redirect(url_for('manage_lists'))
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

# ------------------ Import / Export ------------------
@app.route('/categories/export', methods=['GET'])
def export_categories():
    data = {
        "categories": categories,
        "groups": groups,
        "lists_data": lists_data
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
        lists_data = data.get("lists_data", [])

        # Recompute next IDs
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

        # fix references
        for trx in transactions:
            cid = trx.get('DetectedCategoryId')
            # If no longer valid => None
            if not any(c['id'] == cid for c in categories):
                trx['DetectedCategoryId'] = None

        flash("Imported categories, groups, lists from JSON.", "success")
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
    for c in categories:
        cid = c['id']
        data_list.append({
            "cat_id": cid,
            "name": c['name'],
            "color": c['color'],
            "amount": cat_sums[cid],
            "count": cat_counts[cid]
        })

    if cat_counts[None] > 0:
        data_list.append({
            "cat_id": None,
            "name": "UNK",
            "color": "#dddddd",
            "amount": cat_sums[None],
            "count": cat_counts[None]
        })

    # sorting
    if sort_mode == 'highest':
        data_list.sort(key=lambda x: x['amount'], reverse=True)
    elif sort_mode == 'lowest':
        data_list.sort(key=lambda x: x['amount'])
    elif sort_mode == 'most_transactions':
        data_list.sort(key=lambda x: x['count'], reverse=True)
    elif sort_mode == 'least_transactions':
        data_list.sort(key=lambda x: x['count'])

    group_sums = {}
    group_counts = {}
    for g in groups:
        group_sums[g['id']] = 0.0
        group_counts[g['id']] = 0

    for c in categories:
        amt = cat_sums[c['id']]
        ct = cat_counts[c['id']]
        g_id = c.get('group_id')
        if g_id in group_sums:
            group_sums[g_id] += amt
            group_counts[g_id] += ct

    group_data_list = []
    for g in groups:
        group_data_list.append({
            "group_id": g['id'],
            "name": g['name'],
            "color": g.get('color','#888888'),
            "amount": group_sums[g['id']],
            "count": group_counts[g['id']]
        })

    # categories that show_up_as_group
    for c in categories:
        if c.get('show_up_as_group'):
            group_data_list.append({
                "group_id": -c['id'],
                "name": f"[CatAsGroup] {c['name']}",
                "color": c['color'],
                "amount": cat_sums[c['id']],
                "count": cat_counts[c['id']]
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

    # Accumulate negative amounts per category
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

    # If there are unassigned expenses
    if cat_sums[None] != 0.0:
        cat_names.append("UNK")
        sums.append(cat_sums[None])
        colors.append("#dddddd")

    # If all sums are zero, create a dummy set so we have something to plot
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
    return send_file(out, mimetype='image/png')

@app.route('/stats/group_bar_chart.png')
def group_bar_chart_png():
    # Sum up negative amounts by category
    cat_sums = {}
    for c in categories:
        cat_sums[c['id']] = 0.0

    for trx in transactions:
        if is_expense(trx):
            cid = trx.get('DetectedCategoryId')
            cat_sums.setdefault(cid, 0.0)
            cat_sums[cid] += trx['Betrag']

    # Summarize by group
    group_sums = {}
    color_map = {}
    for g in groups:
        group_sums[g['id']] = 0.0
        color_map[g['id']] = g.get('color','#888888')

    # Accumulate category amounts into their group
    for c in categories:
        g_id = c.get('group_id')
        group_sums.setdefault(g_id, 0.0)
        group_sums[g_id] += cat_sums[c['id']]

    # Handle categories that "show_up_as_group"
    show_as_group = []
    for c in categories:
        if c.get('show_up_as_group'):
            show_as_group.append(("[CatAsGroup] "+c['name'], cat_sums[c['id']], c['color']))

    # Build the data arrays
    group_names = []
    group_vals = []
    color_list = []
    for g in groups:
        group_names.append(g['name'])
        group_vals.append(group_sums[g['id']])
        color_list.append(g['color'])

    # Append the cat-as-group items
    for (lbl, val, col) in show_as_group:
        group_names.append(lbl)
        group_vals.append(val)
        color_list.append(col)

    # If we end up with no data, put a dummy item
    if len(group_names) == 0:
        group_names = ["No data"]
        group_vals = [0]
        color_list = ["#999999"]

    fig, ax = plt.subplots(figsize=(6,4))
    ax.bar(group_names, group_vals, color=color_list)
    ax.set_title("Total Negative Amount by Group")
    ax.set_xlabel("Group")
    ax.set_ylabel("Amount")
    plt.xticks(rotation=45, ha='right')
    plt.tight_layout()

    out = io.BytesIO()
    plt.savefig(out, format='png')
    plt.close(fig)
    out.seek(0)
    return send_file(out, mimetype='image/png')


# ---------------- Downloadable Chart Endpoints ----------------

@app.route('/stats/download_bar_chart')
def download_bar_chart():
    """
    Same logic as bar_chart_png, but served as a file download (attachment).
    """
    # Build the figure in memory
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

    # Return as attachment
    return send_file(
        out,
        mimetype='image/png',
        as_attachment=True,
        download_name='category_chart.png'
    )

@app.route('/stats/download_group_chart')
def download_group_chart():
    """
    Same logic as group_bar_chart_png, but served as a file download (attachment).
    """
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
    """
    Show all transactions that have no DetectedCategoryId (i.e. cat_id=None).
    """
    # Build a list of (index, transaction) for unassigned transactions
    unassigned = []
    for idx, trx in enumerate(transactions):
        if trx.get('DetectedCategoryId') is None:
            unassigned.append((idx, trx))

    # Optionally compute total amounts
    total = 0.0
    refundable = 0.0
    for idx, trx in unassigned:
        amt = trx.get('Betrag', 0.0)
        if amt < 0:  # expense
            total += amt
            # If this transaction is in a "refund_list," include in refundable
            if any(lst['refund_list'] and (idx in lst['transaction_ids']) for lst in lists_data):
                refundable += amt

    after_refund = total - refundable

    return render_template(
        'unassigned_transactions.html', 
        transactions=unassigned,
        total=total,
        refundable=refundable,
        after_refund=after_refund,
        lists_data=lists_data
    )



if __name__ == '__main__':
    logger.debug("Starting Flask app (debug=True) on port 4444.")
    app.run(debug=True, port=4444)

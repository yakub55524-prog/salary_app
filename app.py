import os
from flask import Flask, render_template, request, redirect, url_for
import psycopg2

app = Flask(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable not set")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS smeny (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL,
            point TEXT NOT NULL,
            employee TEXT NOT NULL,
            kol_vo_sotr INTEGER NOT NULL,
            to_brutto REAL NOT NULL,
            summa_dorog REAL NOT NULL,
            kol_vo_dorog INTEGER NOT NULL,
            nastroiki REAL NOT NULL,
            returns REAL NOT NULL
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

MINIMALKAS = {
    'Линия': 20000,
    'Европа': 20000,
    'Маскарад': 15000,
    'Славянка': 15000
}
PROCENTS = {
    'Линия': 0.03,
    'Европа': 0.03,
    'Маскарад': 0.03,
    'Славянка': 0.03
}
POINTS = list(MINIMALKAS.keys())

def get_monthly_totals():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT SUM((to_brutto - returns) / kol_vo_sotr) FROM smeny')
    total_to = cur.fetchone()[0] or 0.0
    cur.execute('''
        SELECT SUM(nastroiki / kol_vo_sotr) 
        FROM (SELECT DISTINCT date, point, nastroiki, kol_vo_sotr FROM smeny) AS t
    ''')
    total_nastroiki = cur.fetchone()[0] or 0.0
    cur.close()
    conn.close()
    return total_to, total_nastroiki

def get_coeffs():
    total_to, total_nastroiki = get_monthly_totals()
    plan_to = 1_350_000
    plan_nastroiki = 88_000
    k_to = 1.05 if total_to >= plan_to else 0.95
    k_nastroiki = 1.1 if total_nastroiki >= plan_nastroiki else 0.9
    return k_to, k_nastroiki

def calculate_salary_row(row, k_to, k_nastroiki, max_mode=False):
    point = row[2]
    employee = row[3]
    kol_vo_sotr = row[4]
    to_brutto = row[5]
    summa_dorog = row[6]
    kol_vo_dorog = row[7]
    nastroiki = row[8]
    returns = row[9]

    chisty_to = to_brutto - returns
    minimalka = chisty_to >= MINIMALKAS.get(point, 20000)
    to_for_proc = chisty_to - summa_dorog
    proc = PROCENTS.get(point, 0.03)

    if minimalka:
        proc_to = proc * to_for_proc
        dolya_nastroek = (nastroiki / 2) / kol_vo_sotr
    else:
        proc_to = 0.0
        dolya_nastroek = 0.0

    bonus_dorog = 500 * kol_vo_dorog
    oklad = 1000

    senior_bonus = 1.1 if employee == "Муслутдинов" else 1.0

    if max_mode:
        premia = (proc_to * 1.05 + dolya_nastroek * 1.1) * senior_bonus
    else:
        premia = (proc_to * k_to + dolya_nastroek * k_nastroiki) * senior_bonus

    return round(oklad + premia + bonus_dorog, 2)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        date = request.form['date']
        point = request.form['point']
        employee = request.form['employee']
        kol_vo_sotr = int(request.form['kol_vo_sotr'])
        to_brutto = float(request.form['to_brutto'])
        summa_dorog = float(request.form['summa_dorog'])
        kol_vo_dorog = int(request.form['kol_vo_dorog'])
        nastroiki = float(request.form['nastroiki'])
        returns = float(request.form['returns'])

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO smeny (date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns))
        conn.commit()
        cur.close()
        conn.close()
        return redirect(url_for('smeny'))
    return render_template('index.html', points=POINTS)

@app.route('/smeny')
def smeny():
    k_to, k_nastroiki = get_coeffs()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM smeny ORDER BY date, point, employee')
    rows = cur.fetchall()
    cur.close()
    conn.close()
    smeny_data = [(row, calculate_salary_row(row, k_to, k_nastroiki, False), calculate_salary_row(row, 1.05, 1.1, True)) for row in rows]
    return render_template('smeny.html', smeny_data=smeny_data, k_to=k_to, k_nastroiki=k_nastroiki)

@app.route('/delete/<int:smena_id>')
def delete_smena(smena_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM smeny WHERE id = %s', (smena_id,))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('smeny'))

@app.route('/itogi')
def itogi():
    k_to, k_nastroiki = get_coeffs()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT employee FROM smeny')
    employees = [row[0] for row in cur.fetchall()]
    results = []
    for emp in employees:
        cur.execute('SELECT * FROM smeny WHERE employee = %s', (emp,))
        rows = cur.fetchall()
        total_real = sum(calculate_salary_row(r, k_to, k_nastroiki, False) for r in rows)
        total_max = sum(calculate_salary_row(r, 1.05, 1.1, True) for r in rows)
        results.append((emp, round(total_real, 2), round(total_max, 2)))
    cur.close()
    conn.close()
    total_to, total_nastroiki = get_monthly_totals()
    plan_to = 1_350_000
    plan_nastroiki = 88_000
    to_ok = total_to >= plan_to
    nastroiki_ok = total_nastroiki >= plan_nastroiki
    return render_template('itogi.html', results=results,
                           total_to=round(total_to, 2),
                           total_nastroiki=round(total_nastroiki, 2),
                           k_to=k_to, k_nastroiki=k_nastroiki,
                           to_ok=to_ok, nastroiki_ok=nastroiki_ok)

@app.route('/clear_all')
def clear_all():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM smeny')
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('itogi'))

# Создаём таблицу при старте (для gunicorn)
init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
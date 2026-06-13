from flask import Flask, render_template, request, redirect, url_for
import sqlite3

app = Flask(__name__)

# --- Инициализация базы данных ---
def init_db():
    conn = sqlite3.connect('salary.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS smeny (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        point TEXT,
        employee TEXT,
        kol_vo_sotr INTEGER,
        to_brutto REAL,
        summa_dorog REAL,
        kol_vo_dorog INTEGER,
        nastroiki REAL,
        returns REAL
    )''')
    conn.commit()
    conn.close()

# --- Справочники (точки) ---
# Минималка и процент по умолчанию (можно расширить)
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
POINTS = list(MINIMALKAS.keys())   # для выпадающего списка

# --- Получение месячных итогов (чистый ТО и настройки) ---
def get_monthly_totals():
    conn = sqlite3.connect('salary.db')
    c = conn.cursor()
    # Чистый ТО (включая дорогостой) – (to_brutto - returns) / kol_vo_sotr
    c.execute('SELECT SUM((to_brutto - returns) / kol_vo_sotr) FROM smeny')
    total_to = c.fetchone()[0] or 0.0
    # Настройки: сумма уникальных настроек за день (nastroiki / kol_vo_sotr)
    c.execute('''
        SELECT SUM(nastroiki / kol_vo_sotr) 
        FROM (SELECT DISTINCT date, point, nastroiki, kol_vo_sotr FROM smeny)
    ''')
    total_nastroiki = c.fetchone()[0] or 0.0
    conn.close()
    return total_to, total_nastroiki

# --- Коэффициенты (ТО и настройки) на основе выполнения планов ---
def get_coeffs():
    total_to, total_nastroiki = get_monthly_totals()
    plan_to = 1_350_000
    plan_nastroiki = 88_000
    k_to = 1.05 if total_to >= plan_to else 0.95
    k_nastroiki = 1.1 if total_nastroiki >= plan_nastroiki else 0.9
    return k_to, k_nastroiki

# --- Расчёт зарплаты для одной смены (строки) ---
def calculate_salary_row(row, k_to, k_nastroiki, max_mode=False):
    # row: (id, date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns)
    point = row[2]
    employee = row[3]
    kol_vo_sotr = row[4]
    to_brutto = row[5]
    summa_dorog = row[6]
    kol_vo_dorog = row[7]
    nastroiki = row[8]
    returns = row[9]

    # Чистый ТО (продажи минус возвраты)
    chisty_to = to_brutto - returns
    # Минималка зависит от точки
    minimalka = chisty_to >= MINIMALKAS.get(point, 20000)
    # ТО для начисления процента (чистый ТО минус сумма дорогостоя)
    to_for_proc = chisty_to - summa_dorog
    # Процент от ТО из справочника
    proc = PROCENTS.get(point, 0.03)

    if minimalka:
        proc_to = proc * to_for_proc
        dolya_nastroek = (nastroiki / 2) / kol_vo_sotr
    else:
        proc_to = 0.0
        dolya_nastroek = 0.0

    # Бонус за дорогостой (каждому сотруднику за каждую единицу)
    bonus_dorog = 500 * kol_vo_dorog
    oklad = 1000

    # Доплата старшему продавцу (Муслутдинов) – 10% к премиальной части
    senior_bonus = 1.1 if employee == "Муслутдинов" else 1.0

    if max_mode:
        # Максимальная зарплата (коэффициенты всегда 1.05 и 1.1)
        premia = (proc_to * 1.05 + dolya_nastroek * 1.1) * senior_bonus
    else:
        # Реальная зарплата с учётом выполнения планов
        premia = (proc_to * k_to + dolya_nastroek * k_nastroiki) * senior_bonus

    total_salary = oklad + premia + bonus_dorog
    return round(total_salary, 2)

# --- Роуты ---
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

        conn = sqlite3.connect('salary.db')
        c = conn.cursor()
        c.execute('''INSERT INTO smeny 
                     (date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns)
                     VALUES (?,?,?,?,?,?,?,?,?)''',
                  (date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns))
        conn.commit()
        conn.close()
        return redirect(url_for('smeny'))
    return render_template('index.html', points=POINTS)

@app.route('/smeny')
def smeny():
    k_to, k_nastroiki = get_coeffs()
    conn = sqlite3.connect('salary.db')
    c = conn.cursor()
    c.execute('SELECT * FROM smeny ORDER BY date, point, employee')
    rows = c.fetchall()
    conn.close()

    smeny_data = []
    for row in rows:
        salary_real = calculate_salary_row(row, k_to, k_nastroiki, max_mode=False)
        salary_max = calculate_salary_row(row, 1.05, 1.1, max_mode=True)
        smeny_data.append((row, salary_real, salary_max))
    return render_template('smeny.html', smeny_data=smeny_data, k_to=k_to, k_nastroiki=k_nastroiki)

@app.route('/delete/<int:smena_id>')
def delete_smena(smena_id):
    conn = sqlite3.connect('salary.db')
    c = conn.cursor()
    c.execute('DELETE FROM smeny WHERE id = ?', (smena_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('smeny'))

@app.route('/itogi')
def itogi():
    k_to, k_nastroiki = get_coeffs()
    conn = sqlite3.connect('salary.db')
    c = conn.cursor()
    c.execute('SELECT DISTINCT employee FROM smeny')
    employees = [row[0] for row in c.fetchall()]

    results = []
    for emp in employees:
        c.execute('SELECT * FROM smeny WHERE employee = ?', (emp,))
        rows = c.fetchall()
        total_real = 0.0
        total_max = 0.0
        for row in rows:
            total_real += calculate_salary_row(row, k_to, k_nastroiki, max_mode=False)
            total_max += calculate_salary_row(row, 1.05, 1.1, max_mode=True)
        results.append((emp, round(total_real, 2), round(total_max, 2)))

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
    conn = sqlite3.connect('salary.db')
    c = conn.cursor()
    c.execute('DELETE FROM smeny')
    conn.commit()
    conn.close()
    return redirect(url_for('itogi'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
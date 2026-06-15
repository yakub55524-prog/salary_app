import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-me')

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable not set")

# ---------- Настройка Flask-Login ----------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


# ---------- Модель пользователя ----------
class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id
        self.username = username
        self.role = role  # 'admin' или 'user'


@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, username, role FROM users WHERE id = %s", (user_id,))
    user_data = cur.fetchone()
    cur.close()
    conn.close()
    if user_data:
        return User(user_data['id'], user_data['username'], user_data['role'])
    return None


# ---------- Работа с базой данных ----------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Таблица пользователей
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user'
        )
    ''')
    # Таблица смен (добавляем user_id)
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
            returns REAL NOT NULL,
            user_id INTEGER REFERENCES users(id)
        )
    ''')
    # Если столбец user_id отсутствует (старая БД), добавляем
    try:
        cur.execute("ALTER TABLE smeny ADD COLUMN user_id INTEGER REFERENCES users(id);")
    except:
        pass
    conn.commit()
    cur.close()
    conn.close()


# ---------- Функции расчёта (без изменений) ----------
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


def get_monthly_totals(user_id=None):
    conn = get_db_connection()
    cur = conn.cursor()
    if user_id:
        cur.execute('SELECT SUM((to_brutto - returns) / kol_vo_sotr) FROM smeny WHERE user_id = %s', (user_id,))
        total_to = cur.fetchone()[0] or 0.0
        cur.execute('''
            SELECT SUM(nastroiki / kol_vo_sotr) 
            FROM (SELECT DISTINCT date, point, nastroiki, kol_vo_sotr FROM smeny WHERE user_id = %s) AS t
        ''', (user_id,))
        total_nastroiki = cur.fetchone()[0] or 0.0
    else:
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


def get_coeffs(user_id=None):
    total_to, total_nastroiki = get_monthly_totals(user_id)
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


# ---------- Маршруты аутентификации ----------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        hashed = generate_password_hash(password)

        conn = get_db_connection()
        cur = conn.cursor()
        # Первый пользователь становится администратором
        cur.execute("SELECT COUNT(*) FROM users")
        count = cur.fetchone()[0]
        role = 'admin' if count == 0 else 'user'

        try:
            cur.execute("INSERT INTO users (username, password, role) VALUES (%s, %s, %s)",
                        (username, hashed, role))
            conn.commit()
            flash('Регистрация успешна. Теперь войдите.', 'success')
            return redirect(url_for('login'))
        except psycopg2.IntegrityError:
            flash('Пользователь с таким именем уже существует.', 'danger')
        finally:
            cur.close()
            conn.close()
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if user and check_password_hash(user['password'], password):
            login_user(User(user['id'], user['username'], user['role']))
            return redirect(url_for('index'))
        else:
            flash('Неверное имя или пароль', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ---------- Основные маршруты ----------
@app.route('/', methods=['GET', 'POST'])
@login_required
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
            INSERT INTO smeny (date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns,
              current_user.id))
        conn.commit()
        cur.close()
        conn.close()
        return redirect(url_for('smeny'))
    return render_template('index.html', points=POINTS)


@app.route('/smeny')
@login_required
def smeny():
    if current_user.role == 'admin':
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT * FROM smeny ORDER BY date, point, employee')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        k_to, k_nastroiki = get_coeffs()  # для админа – по всем данным
    else:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT * FROM smeny WHERE user_id = %s ORDER BY date, point, employee', (current_user.id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        k_to, k_nastroiki = get_coeffs(user_id=current_user.id)

    smeny_data = []
    for row in rows:
        salary_real = calculate_salary_row(row, k_to, k_nastroiki, False)
        salary_max = calculate_salary_row(row, 1.05, 1.1, True)
        smeny_data.append((row, salary_real, salary_max))
    return render_template('smeny.html', smeny_data=smeny_data, k_to=k_to, k_nastroiki=k_nastroiki,
                           role=current_user.role)


@app.route('/edit/<int:smena_id>', methods=['GET', 'POST'])
@login_required
def edit_smena(smena_id):
    # Проверка прав: администратор может всё, обычный пользователь – только свои смены
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM smeny WHERE id = %s', (smena_id,))
    smena = cur.fetchone()
    cur.close()
    conn.close()
    if not smena:
        flash('Смена не найдена', 'danger')
        return redirect(url_for('smeny'))
    if current_user.role != 'admin' and smena[10] != current_user.id:
        flash('У вас нет прав для редактирования этой смены', 'danger')
        return redirect(url_for('smeny'))

    if request.method == 'POST':
        # Получаем обновлённые данные (можно ограничиться только возвратами, но для полноты – все поля)
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
            UPDATE smeny SET date=%s, point=%s, employee=%s, kol_vo_sotr=%s,
            to_brutto=%s, summa_dorog=%s, kol_vo_dorog=%s, nastroiki=%s, returns=%s
            WHERE id=%s
        ''', (date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns, smena_id))
        conn.commit()
        cur.close()
        conn.close()
        flash('Смена успешно обновлена', 'success')
        return redirect(url_for('smeny'))

    # GET: показываем форму с текущими данными
    return render_template('edit.html', smena=smena, points=POINTS)


@app.route('/delete/<int:smena_id>')
@login_required
def delete_smena(smena_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM smeny WHERE id = %s', (smena_id,))
    row = cur.fetchone()
    if not row:
        flash('Смена не найдена', 'danger')
        return redirect(url_for('smeny'))
    if current_user.role != 'admin' and row[0] != current_user.id:
        flash('У вас нет прав для удаления этой смены', 'danger')
        return redirect(url_for('smeny'))
    cur.execute('DELETE FROM smeny WHERE id = %s', (smena_id,))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('smeny'))


@app.route('/itogi')
@login_required
def itogi():
    if current_user.role == 'admin':
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT DISTINCT employee FROM smeny')
        employees = [row[0] for row in cur.fetchall()]
        results = []
        for emp in employees:
            cur.execute('SELECT * FROM smeny WHERE employee = %s', (emp,))
            rows = cur.fetchall()
            k_to, k_nastroiki = get_coeffs()  # общие коэффициенты
            total_real = sum(calculate_salary_row(r, k_to, k_nastroiki, False) for r in rows)
            total_max = sum(calculate_salary_row(r, 1.05, 1.1, True) for r in rows)
            results.append((emp, round(total_real, 2), round(total_max, 2)))
        cur.close()
        conn.close()
        total_to, total_nastroiki = get_monthly_totals()
        to_ok = total_to >= 1_350_000
        nastroiki_ok = total_nastroiki >= 88_000
        k_to, k_nastroiki = get_coeffs()
    else:
        # Обычный пользователь видит только себя
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT DISTINCT employee FROM smeny WHERE user_id = %s', (current_user.id,))
        employees = [row[0] for row in cur.fetchall()]
        results = []
        for emp in employees:
            cur.execute('SELECT * FROM smeny WHERE employee = %s AND user_id = %s', (emp, current_user.id))
            rows = cur.fetchall()
            k_to, k_nastroiki = get_coeffs(user_id=current_user.id)
            total_real = sum(calculate_salary_row(r, k_to, k_nastroiki, False) for r in rows)
            total_max = sum(calculate_salary_row(r, 1.05, 1.1, True) for r in rows)
            results.append((emp, round(total_real, 2), round(total_max, 2)))
        cur.close()
        conn.close()
        total_to, total_nastroiki = get_monthly_totals(user_id=current_user.id)
        to_ok = total_to >= 1_350_000
        nastroiki_ok = total_nastroiki >= 88_000
        k_to, k_nastroiki = get_coeffs(user_id=current_user.id)

    return render_template('itogi.html', results=results,
                           total_to=round(total_to, 2),
                           total_nastroiki=round(total_nastroiki, 2),
                           k_to=k_to, k_nastroiki=k_nastroiki,
                           to_ok=to_ok, nastroiki_ok=nastroiki_ok,
                           role=current_user.role)


@app.route('/clear_all')
@login_required
def clear_all():
    if current_user.role != 'admin':
        flash('Только администратор может очищать все данные', 'danger')
        return redirect(url_for('itogi'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM smeny')
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('itogi'))


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
import os
import logging
import re
from datetime import datetime, timedelta
import telebot
from telebot import types
import psycopg2
from psycopg2.extras import RealDictCursor

# --- Настройка логирования ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Переменные окружения ---
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

# --- Подключение к БД ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

# --- Функции работы с пользователями ---
def get_user_by_telegram_id(telegram_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, username, role FROM users WHERE telegram_id = %s", (str(telegram_id),))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def get_user_by_username(username):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, username, role FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def update_telegram_id(user_id, telegram_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET telegram_id = %s WHERE id = %s", (str(telegram_id), user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_or_create_user(telegram_id, username):
    user = get_user_by_telegram_id(telegram_id)
    if user:
        return user
    user = get_user_by_username(username)
    if user:
        update_telegram_id(user['id'], telegram_id)
        return user
    return None

# --- Функции расчёта (скопированы из app.py) ---
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

# --- Функции работы со сменами ---
def get_shifts_for_user(user_id, date=None):
    conn = get_db_connection()
    cur = conn.cursor()
    if date:
        cur.execute('SELECT * FROM smeny WHERE user_id = %s AND date = %s ORDER BY date, point, employee', (user_id, date))
    else:
        cur.execute('SELECT * FROM smeny WHERE user_id = %s ORDER BY date, point, employee', (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def get_shift_by_date_point(user_id, date, point, employee):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM smeny WHERE user_id = %s AND date = %s AND point = %s AND employee = %s', (user_id, date, point, employee))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def update_shift_returns(smena_id, new_returns):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE smeny SET returns = %s WHERE id = %s', (new_returns, smena_id))
    conn.commit()
    cur.close()
    conn.close()

def add_shift(user_id, date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO smeny (date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns, user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns, user_id))
    conn.commit()
    cur.close()
    conn.close()

# --- Создание бота ---
bot = telebot.TeleBot(TOKEN)

# --- Состояния для пользовательских сессий (хранятся в словаре) ---
user_data = {}

# --- Команды ---
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    db_user = get_user_by_telegram_id(user_id)
    if db_user:
        bot.reply_to(message, f"Добро пожаловать, {db_user['username']}! Ваша роль: {db_user['role']}\nИспользуйте /help для списка команд.")
    else:
        bot.reply_to(message, "Добро пожаловать! Для начала работы укажите ваше имя пользователя (логин) из веб-приложения.\nВведите /set_username <ваш_логин>")

@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = """
Доступные команды:
/start - Начать работу
/set_username <логин> - Привязать Telegram к пользователю
/add_shift - Добавить новую смену (пошагово)
/add_return - Добавить возврат к существующей смене
/my_shifts - Показать мои смены за текущий месяц
/all_shifts - (админ) Показать все смены
/itogi - Показать итоги за месяц
/help - Это сообщение
/cancel - Отменить текущую операцию
"""
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['cancel'])
def cancel(message):
    user_id = message.from_user.id
    if user_id in user_data:
        del user_data[user_id]
    bot.reply_to(message, "Операция отменена.")

@bot.message_handler(commands=['set_username'])
def set_username(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Пожалуйста, укажите имя пользователя после команды, например: /set_username admin")
        return
    username = args[1]
    user = get_user_by_username(username)
    if not user:
        bot.reply_to(message, "Пользователь с таким именем не найден. Зарегистрируйтесь сначала в веб-приложении.")
        return
    telegram_id = message.from_user.id
    update_telegram_id(user['id'], telegram_id)
    bot.reply_to(message, f"Имя пользователя {username} успешно привязано к вашему Telegram аккаунту! Теперь вы можете использовать все команды.")

# --- Добавление смены (пошагово) ---
@bot.message_handler(commands=['add_shift'])
def add_shift_start(message):
    user_id = message.from_user.id
    db_user = get_user_by_telegram_id(user_id)
    if not db_user:
        bot.reply_to(message, "Сначала привяжите аккаунт командой /set_username")
        return
    # Инициализируем данные для пользователя
    user_data[user_id] = {'step': 'date', 'user_id': db_user['id'], 'employee_name': db_user['username']}
    bot.reply_to(message, "Введите дату смены в формате ГГГГ-ММ-ДД (например, 2026-06-01):")

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'date')
def process_date(message):
    user_id = message.from_user.id
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        bot.reply_to(message, "Неверный формат. Введите дату в формате ГГГГ-ММ-ДД:")
        return
    user_data[user_id]['date'] = date_str
    # Спросим точку с помощью кнопок
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    for point in POINTS:
        keyboard.add(types.InlineKeyboardButton(point, callback_data=f"point_{point}"))
    bot.reply_to(message, "Выберите точку:", reply_markup=keyboard)
    user_data[user_id]['step'] = 'point'

@bot.callback_query_handler(func=lambda call: call.data.startswith('point_'))
def process_point(call):
    user_id = call.from_user.id
    if user_id not in user_data or user_data[user_id]['step'] != 'point':
        bot.answer_callback_query(call.id, "Операция устарела, начните заново /add_shift")
        return
    point = call.data.split('_')[1]
    user_data[user_id]['point'] = point
    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"Выбрана точка: {point}\nТеперь введите сотрудника (если оставить пустым, будет использован ваш логин):")
    user_data[user_id]['step'] = 'employee'
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'employee')
def process_employee(message):
    user_id = message.from_user.id
    employee = message.text.strip()
    if not employee:
        employee = user_data[user_id].get('employee_name', 'Неизвестно')
    user_data[user_id]['employee'] = employee
    bot.reply_to(message, "Введите количество сотрудников в смене (число):")
    user_data[user_id]['step'] = 'kol_vo_sotr'

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'kol_vo_sotr')
def process_kolvo(message):
    user_id = message.from_user.id
    try:
        kol = int(message.text.strip())
        if kol <= 0:
            raise ValueError
        user_data[user_id]['kol_vo_sotr'] = kol
    except ValueError:
        bot.reply_to(message, "Введите положительное целое число:")
        return
    bot.reply_to(message, "Введите ТО брутто (сумма продаж, ₽):")
    user_data[user_id]['step'] = 'to_brutto'

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'to_brutto')
def process_to(message):
    user_id = message.from_user.id
    try:
        to = float(message.text.strip())
        if to < 0:
            raise ValueError
        user_data[user_id]['to_brutto'] = to
    except ValueError:
        bot.reply_to(message, "Введите число >= 0:")
        return
    bot.reply_to(message, "Введите сумму дорогостоя (₽) (если нет, введите 0):")
    user_data[user_id]['step'] = 'summa_dorog'

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'summa_dorog')
def process_summa_dorog(message):
    user_id = message.from_user.id
    try:
        summa = float(message.text.strip())
        if summa < 0:
            raise ValueError
        user_data[user_id]['summa_dorog'] = summa
    except ValueError:
        bot.reply_to(message, "Введите число >= 0:")
        return
    bot.reply_to(message, "Введите количество дорогостоящих товаров (шт) (если нет, 0):")
    user_data[user_id]['step'] = 'kol_vo_dorog'

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'kol_vo_dorog')
def process_kolvo_dorog(message):
    user_id = message.from_user.id
    try:
        kol = int(message.text.strip())
        if kol < 0:
            raise ValueError
        user_data[user_id]['kol_vo_dorog'] = kol
    except ValueError:
        bot.reply_to(message, "Введите целое число >= 0:")
        return
    bot.reply_to(message, "Введите сумму настроек за день (₽):")
    user_data[user_id]['step'] = 'nastroiki'

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'nastroiki')
def process_nastroiki(message):
    user_id = message.from_user.id
    try:
        nastroiki = float(message.text.strip())
        if nastroiki < 0:
            raise ValueError
        user_data[user_id]['nastroiki'] = nastroiki
    except ValueError:
        bot.reply_to(message, "Введите число >= 0:")
        return
    bot.reply_to(message, "Введите сумму возвратов за день (₽) (если нет, 0):")
    user_data[user_id]['step'] = 'returns'

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'returns')
def process_returns(message):
    user_id = message.from_user.id
    try:
        returns = float(message.text.strip())
        if returns < 0:
            raise ValueError
        user_data[user_id]['returns'] = returns
    except ValueError:
        bot.reply_to(message, "Введите число >= 0:")
        return

    # Сохраняем смену
    data = user_data[user_id]
    existing = get_shift_by_date_point(data['user_id'], data['date'], data['point'], data['employee'])
    if existing:
        bot.reply_to(message, "Смена за эту дату, точку и сотрудника уже существует. Чтобы изменить данные, удалите старую через веб-интерфейс или используйте /add_return для добавления возвратов.")
        del user_data[user_id]
        return

    add_shift(data['user_id'], data['date'], data['point'], data['employee'],
              data['kol_vo_sotr'], data['to_brutto'], data['summa_dorog'],
              data['kol_vo_dorog'], data['nastroiki'], data['returns'])
    bot.reply_to(message, f"✅ Смена добавлена!\nДата: {data['date']}\nТочка: {data['point']}\nСотрудник: {data['employee']}\nТО брутто: {data['to_brutto']} ₽\nНастройки: {data['nastroiki']} ₽\nВозвраты: {data['returns']} ₽")
    del user_data[user_id]

# --- Добавление возврата (упрощённо) ---
@bot.message_handler(commands=['add_return'])
def add_return_start(message):
    user_id = message.from_user.id
    db_user = get_user_by_telegram_id(user_id)
    if not db_user:
        bot.reply_to(message, "Сначала привяжите аккаунт командой /set_username")
        return
    # Используем тот же словарь для временного хранения
    user_data[user_id] = {'step': 'return_date', 'user_id': db_user['id']}
    bot.reply_to(message, "Введите дату смены, к которой добавляем возврат (ГГГГ-ММ-ДД):")

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'return_date')
def process_return_date(message):
    user_id = message.from_user.id
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        bot.reply_to(message, "Неверный формат. Введите дату в формате ГГГГ-ММ-ДД:")
        return
    user_data[user_id]['return_date'] = date_str
    # Найдём смены за эту дату для пользователя
    rows = get_shifts_for_user(user_data[user_id]['user_id'], date_str)
    if not rows:
        bot.reply_to(message, "Смен за эту дату не найдено. Сначала добавьте смену через /add_shift.")
        del user_data[user_id]
        return
    if len(rows) == 1:
        user_data[user_id]['smena_id'] = rows[0][0]
        bot.reply_to(message, f"Найдена смена: {rows[0][1]} {rows[0][2]} {rows[0][3]}. Текущие возвраты: {rows[0][9]} ₽. Введите сумму возврата, которую нужно добавить:")
        user_data[user_id]['step'] = 'return_amount'
    else:
        # Несколько смен – упростим: предложим выбрать по номеру
        options = []
        for idx, row in enumerate(rows, start=1):
            options.append(f"{idx}: {row[2]} - {row[3]} (возвраты: {row[9]})")
        bot.reply_to(message, "Найдено несколько смен за эту дату. Пока редактирование нескольких смен не реализовано. Используйте веб-интерфейс или укажите точную дату и сотрудника.\n" + "\n".join(options))
        del user_data[user_id]

@bot.message_handler(func=lambda message: message.from_user.id in user_data and user_data[message.from_user.id]['step'] == 'return_amount')
def process_return_amount(message):
    user_id = message.from_user.id
    try:
        amount = float(message.text.strip())
        if amount < 0:
            raise ValueError
    except ValueError:
        bot.reply_to(message, "Введите положительное число:")
        return

    smena_id = user_data[user_id]['smena_id']
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT returns FROM smeny WHERE id = %s', (smena_id,))
    current = cur.fetchone()[0] or 0.0
    new_returns = current + amount
    update_shift_returns(smena_id, new_returns)
    cur.close()
    conn.close()
    bot.reply_to(message, f"✅ Возврат добавлен! Новые общие возвраты за эту смену: {new_returns} ₽.")
    del user_data[user_id]

# --- Просмотр смен ---
@bot.message_handler(commands=['my_shifts'])
def my_shifts(message):
    user_id = message.from_user.id
    db_user = get_user_by_telegram_id(user_id)
    if not db_user:
        bot.reply_to(message, "Сначала привяжите аккаунт командой /set_username")
        return
    rows = get_shifts_for_user(db_user['id'])
    if not rows:
        bot.reply_to(message, "У вас пока нет смен за этот месяц.")
        return
    text = "Ваши смены за текущий месяц:\n\n"
    for row in rows:
        text += f"{row[1]} {row[2]} {row[3]} - ТО брутто: {row[5]} ₽, Настройки: {row[8]} ₽, Возвраты: {row[9]} ₽\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=['all_shifts'])
def all_shifts(message):
    user_id = message.from_user.id
    db_user = get_user_by_telegram_id(user_id)
    if not db_user or db_user['role'] != 'admin':
        bot.reply_to(message, "У вас нет прав для просмотра всех смен.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM smeny ORDER BY date, point, employee')
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        bot.reply_to(message, "Смен пока нет.")
        return
    text = "Все смены:\n\n"
    for row in rows:
        text += f"{row[1]} {row[2]} {row[3]} - ТО брутто: {row[5]} ₽, Возвраты: {row[9]} ₽, ЗП факт: {calculate_salary_row(row, *get_coeffs(user_id=None), max_mode=False)} ₽\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=['itogi'])
def itogi(message):
    user_id = message.from_user.id
    db_user = get_user_by_telegram_id(user_id)
    if not db_user:
        bot.reply_to(message, "Сначала привяжите аккаунт командой /set_username")
        return
    if db_user['role'] == 'admin':
        # Для админа покажем итоги по всем сотрудникам
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT DISTINCT employee FROM smeny')
        employees = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        text = "Итоговая зарплата (админ):\n"
        for emp in employees:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute('SELECT * FROM smeny WHERE employee = %s', (emp,))
            rows = cur.fetchall()
            cur.close()
            conn.close()
            total = 0
            for row in rows:
                total += calculate_salary_row(row, *get_coeffs(user_id=None), max_mode=False)
            text += f"{emp}: {total} ₽\n"
        bot.reply_to(message, text)
    else:
        rows = get_shifts_for_user(db_user['id'])
        if not rows:
            bot.reply_to(message, "У вас пока нет смен.")
            return
        total = 0
        for row in rows:
            total += calculate_salary_row(row, *get_coeffs(user_id=db_user['id']), max_mode=False)
        bot.reply_to(message, f"Ваша итоговая зарплата за месяц: {total} ₽")

# --- Запуск бота ---
if __name__ == '__main__':
    logger.info("Bot started polling...")
    bot.infinity_polling()
import logging
import os
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler

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
    """Проверяем, есть ли пользователь с таким telegram_id или username, если нет - создаём"""
    user = get_user_by_telegram_id(telegram_id)
    if user:
        return user
    user = get_user_by_username(username)
    if user:
        update_telegram_id(user['id'], telegram_id)
        return user
    return None  # пользователь не найден, нужно зарегистрироваться через веб-приложение

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

# --- Функции для работы со сменами ---
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

# --- Состояния для ConversationHandler ---
(USERNAME, SHIFT_DATE, SHIFT_POINT, SHIFT_EMPLOYEE, SHIFT_KOLVO, SHIFT_TO, SHIFT_SUMMA_DOROG, SHIFT_KOLVO_DOROG, SHIFT_NASTROIKI, SHIFT_RETURNS,
 RETURN_DATE, RETURN_AMOUNT) = range(12)

# --- Функции команд ---
async def start(update: Update, context):
    user = update.effective_user
    telegram_id = user.id
    # Проверяем, есть ли пользователь в БД
    db_user = get_user_by_telegram_id(telegram_id)
    if db_user:
        await update.message.reply_text(f"Добро пожаловать, {db_user['username']}!\nВаша роль: {db_user['role']}\nИспользуйте /help для списка команд.")
    else:
        await update.message.reply_text("Добро пожаловать! Для начала работы укажите ваше имя пользователя (логин) из веб-приложения.\nВведите /set_username <ваш_логин>")
    return ConversationHandler.END

async def set_username(update: Update, context):
    args = context.args
    if not args:
        await update.message.reply_text("Пожалуйста, укажите имя пользователя после команды, например: /set_username admin")
        return
    username = args[0]
    user = get_user_by_username(username)
    if not user:
        await update.message.reply_text("Пользователь с таким именем не найден. Зарегистрируйтесь сначала в веб-приложении.")
        return
    telegram_id = update.effective_user.id
    update_telegram_id(user['id'], telegram_id)
    await update.message.reply_text(f"Имя пользователя {username} успешно привязано к вашему Telegram аккаунту! Теперь вы можете использовать все команды.")

async def help_command(update: Update, context):
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
    """
    await update.message.reply_text(help_text)

async def cancel(update: Update, context):
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# --- Обработчик добавления смены ---
async def add_shift_start(update: Update, context):
    user = update.effective_user
    db_user = get_user_by_telegram_id(user.id)
    if not db_user:
        await update.message.reply_text("Сначала привяжите аккаунт командой /set_username")
        return ConversationHandler.END
    context.user_data['user_id'] = db_user['id']
    context.user_data['employee_name'] = db_user['username']  # по умолчанию, но можно будет изменить
    await update.message.reply_text("Введите дату смены в формате ГГГГ-ММ-ДД (например, 2026-06-01):")
    return SHIFT_DATE

async def shift_date(update: Update, context):
    date_str = update.message.text.strip()
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите дату в формате ГГГГ-ММ-ДД:")
        return SHIFT_DATE
    context.user_data['date'] = date_str
    # Спросим точку
    keyboard = [[InlineKeyboardButton(point, callback_data=f'point_{point}')] for point in POINTS]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите точку:", reply_markup=reply_markup)
    return SHIFT_POINT

async def shift_point(update: Update, context):
    query = update.callback_query
    await query.answer()
    point = query.data.split('_')[1]
    context.user_data['point'] = point
    await query.edit_message_text(f"Выбрана точка: {point}\nТеперь введите сотрудника (если оставить пустым, будет использован ваш логин):")
    return SHIFT_EMPLOYEE

async def shift_employee(update: Update, context):
    employee = update.message.text.strip()
    if not employee:
        employee = context.user_data.get('employee_name', 'Неизвестно')
    context.user_data['employee'] = employee
    await update.message.reply_text("Введите количество сотрудников в смене (число):")
    return SHIFT_KOLVO

async def shift_kolvo(update: Update, context):
    try:
        kol = int(update.message.text.strip())
        if kol <= 0:
            raise ValueError
        context.user_data['kol_vo_sotr'] = kol
    except ValueError:
        await update.message.reply_text("Введите положительное целое число:")
        return SHIFT_KOLVO
    await update.message.reply_text("Введите ТО брутто (сумма продаж, ₽):")
    return SHIFT_TO

async def shift_to(update: Update, context):
    try:
        to = float(update.message.text.strip())
        if to < 0:
            raise ValueError
        context.user_data['to_brutto'] = to
    except ValueError:
        await update.message.reply_text("Введите число >= 0:")
        return SHIFT_TO
    await update.message.reply_text("Введите сумму дорогостоя (₽) (если нет, введите 0):")
    return SHIFT_SUMMA_DOROG

async def shift_summa_dorog(update: Update, context):
    try:
        summa = float(update.message.text.strip())
        if summa < 0:
            raise ValueError
        context.user_data['summa_dorog'] = summa
    except ValueError:
        await update.message.reply_text("Введите число >= 0:")
        return SHIFT_SUMMA_DOROG
    await update.message.reply_text("Введите количество дорогостоящих товаров (шт) (если нет, 0):")
    return SHIFT_KOLVO_DOROG

async def shift_kolvo_dorog(update: Update, context):
    try:
        kol = int(update.message.text.strip())
        if kol < 0:
            raise ValueError
        context.user_data['kol_vo_dorog'] = kol
    except ValueError:
        await update.message.reply_text("Введите целое число >= 0:")
        return SHIFT_KOLVO_DOROG
    await update.message.reply_text("Введите сумму настроек за день (₽):")
    return SHIFT_NASTROIKI

async def shift_nastroiki(update: Update, context):
    try:
        nastroiki = float(update.message.text.strip())
        if nastroiki < 0:
            raise ValueError
        context.user_data['nastroiki'] = nastroiki
    except ValueError:
        await update.message.reply_text("Введите число >= 0:")
        return SHIFT_NASTROIKI
    await update.message.reply_text("Введите сумму возвратов за день (₽) (если нет, 0):")
    return SHIFT_RETURNS

async def shift_returns(update: Update, context):
    try:
        returns = float(update.message.text.strip())
        if returns < 0:
            raise ValueError
        context.user_data['returns'] = returns
    except ValueError:
        await update.message.reply_text("Введите число >= 0:")
        return SHIFT_RETURNS

    # Сохраняем смену
    user_id = context.user_data['user_id']
    date = context.user_data['date']
    point = context.user_data['point']
    employee = context.user_data['employee']
    kol_vo_sotr = context.user_data['kol_vo_sotr']
    to_brutto = context.user_data['to_brutto']
    summa_dorog = context.user_data['summa_dorog']
    kol_vo_dorog = context.user_data['kol_vo_dorog']
    nastroiki = context.user_data['nastroiki']
    returns = context.user_data['returns']

    # Проверяем, нет ли уже такой смены
    existing = get_shift_by_date_point(user_id, date, point, employee)
    if existing:
        await update.message.reply_text("Смена за эту дату, точку и сотрудника уже существует. Хотите обновить данные? (пока не реализовано, удалите старую через веб-интерфейс)")
        return ConversationHandler.END

    add_shift(user_id, date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns)
    await update.message.reply_text(f"✅ Смена добавлена!\nДата: {date}\nТочка: {point}\nСотрудник: {employee}\nТО брутто: {to_brutto} ₽\nНастройки: {nastroiki} ₽\nВозвраты: {returns} ₽")
    return ConversationHandler.END

# --- Обработчик добавления возврата ---
async def add_return_start(update: Update, context):
    user = update.effective_user
    db_user = get_user_by_telegram_id(user.id)
    if not db_user:
        await update.message.reply_text("Сначала привяжите аккаунт командой /set_username")
        return ConversationHandler.END
    context.user_data['user_id'] = db_user['id']
    await update.message.reply_text("Введите дату смены, к которой добавляем возврат (ГГГГ-ММ-ДД):")
    return RETURN_DATE

async def return_date(update: Update, context):
    date_str = update.message.text.strip()
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите дату в формате ГГГГ-ММ-ДД:")
        return RETURN_DATE
    context.user_data['return_date'] = date_str
    # Найдём смены за эту дату для пользователя
    user_id = context.user_data['user_id']
    rows = get_shifts_for_user(user_id, date_str)
    if not rows:
        await update.message.reply_text("Смен за эту дату не найдено. Сначала добавьте смену через /add_shift.")
        return ConversationHandler.END
    if len(rows) == 1:
        context.user_data['smena_id'] = rows[0][0]
        await update.message.reply_text(f"Найдена смена: {rows[0][1]} {rows[0][2]} {rows[0][3]}. Текущие возвраты: {rows[0][9]} ₽. Введите сумму возврата, которую нужно добавить:")
        return RETURN_AMOUNT
    else:
        # Несколько смен за день – уточним точку и сотрудника
        options = []
        for row in rows:
            options.append(f"{row[2]} - {row[3]} (возвраты: {row[9]})")
        await update.message.reply_text("Найдено несколько смен за эту дату. Уточните, какую смену редактировать? (пока не реализовано, выберите в веб-интерфейсе)")
        return ConversationHandler.END

async def return_amount(update: Update, context):
    try:
        amount = float(update.message.text.strip())
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите положительное число:")
        return RETURN_AMOUNT

    smena_id = context.user_data['smena_id']
    # Получаем текущие возвраты
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT returns FROM smeny WHERE id = %s', (smena_id,))
    current = cur.fetchone()[0] or 0.0
    new_returns = current + amount
    update_shift_returns(smena_id, new_returns)
    cur.close()
    conn.close()
    await update.message.reply_text(f"✅ Возврат добавлен! Новые общие возвраты за эту смену: {new_returns} ₽.")
    return ConversationHandler.END

# --- Просмотр смен ---
async def my_shifts(update: Update, context):
    user = update.effective_user
    db_user = get_user_by_telegram_id(user.id)
    if not db_user:
        await update.message.reply_text("Сначала привяжите аккаунт командой /set_username")
        return
    rows = get_shifts_for_user(db_user['id'])
    if not rows:
        await update.message.reply_text("У вас пока нет смен за этот месяц.")
        return
    text = "Ваши смены за текущий месяц:\n\n"
    for row in rows:
        text += f"{row[1]} {row[2]} {row[3]} - ТО брутто: {row[5]} ₽, Настройки: {row[8]} ₽, Возвраты: {row[9]} ₽\n"
    await update.message.reply_text(text)

async def all_shifts(update: Update, context):
    user = update.effective_user
    db_user = get_user_by_telegram_id(user.id)
    if not db_user or db_user['role'] != 'admin':
        await update.message.reply_text("У вас нет прав для просмотра всех смен.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM smeny ORDER BY date, point, employee')
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        await update.message.reply_text("Смен пока нет.")
        return
    text = "Все смены:\n\n"
    for row in rows:
        text += f"{row[1]} {row[2]} {row[3]} - ТО брутто: {row[5]} ₽, Возвраты: {row[9]} ₽, ЗП факт: {calculate_salary_row(row, *get_coeffs(user_id=None), max_mode=False)} ₽\n"
    await update.message.reply_text(text)

async def itogi(update: Update, context):
    user = update.effective_user
    db_user = get_user_by_telegram_id(user.id)
    if not db_user:
        await update.message.reply_text("Сначала привяжите аккаунт командой /set_username")
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
            # Получим все смены этого сотрудника
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
        await update.message.reply_text(text)
    else:
        # Обычный пользователь – свои итоги
        rows = get_shifts_for_user(db_user['id'])
        if not rows:
            await update.message.reply_text("У вас пока нет смен.")
            return
        total = 0
        for row in rows:
            total += calculate_salary_row(row, *get_coeffs(user_id=db_user['id']), max_mode=False)
        await update.message.reply_text(f"Ваша итоговая зарплата за месяц: {total} ₽")

# --- Основная функция ---
def main():
    application = Application.builder().token(TOKEN).build()

    # Обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("set_username", set_username))
    application.add_handler(CommandHandler("my_shifts", my_shifts))
    application.add_handler(CommandHandler("all_shifts", all_shifts))
    application.add_handler(CommandHandler("itogi", itogi))

    # Добавление смены - ConversationHandler
    add_shift_conv = ConversationHandler(
        entry_points=[CommandHandler('add_shift', add_shift_start)],
        states={
            SHIFT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, shift_date)],
            SHIFT_POINT: [CallbackQueryHandler(shift_point, pattern='^point_')],
            SHIFT_EMPLOYEE: [MessageHandler(filters.TEXT & ~filters.COMMAND, shift_employee)],
            SHIFT_KOLVO: [MessageHandler(filters.TEXT & ~filters.COMMAND, shift_kolvo)],
            SHIFT_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, shift_to)],
            SHIFT_SUMMA_DOROG: [MessageHandler(filters.TEXT & ~filters.COMMAND, shift_summa_dorog)],
            SHIFT_KOLVO_DOROG: [MessageHandler(filters.TEXT & ~filters.COMMAND, shift_kolvo_dorog)],
            SHIFT_NASTROIKI: [MessageHandler(filters.TEXT & ~filters.COMMAND, shift_nastroiki)],
            SHIFT_RETURNS: [MessageHandler(filters.TEXT & ~filters.COMMAND, shift_returns)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(add_shift_conv)

    # Добавление возврата - ConversationHandler
    add_return_conv = ConversationHandler(
        entry_points=[CommandHandler('add_return', add_return_start)],
        states={
            RETURN_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, return_date)],
            RETURN_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, return_amount)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(add_return_conv)

    # Запуск бота
    application.run_polling()

if __name__ == '__main__':
    main()
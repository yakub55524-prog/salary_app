import os
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import psycopg2
from psycopg2.extras import RealDictCursor

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токен
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

# --- Подключение к БД ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

# --- Функции работы с пользователями (аналогичны предыдущим) ---
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

# --- Функции расчёта (копия из app.py) ---
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

# --- Функции работы со сменами (аналогичны) ---
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

# --- Состояния для FSM ---
class AddShift(StatesGroup):
    date = State()
    point = State()
    employee = State()
    kol_vo = State()
    to_brutto = State()
    summa_dorog = State()
    kol_vo_dorog = State()
    nastroiki = State()
    returns = State()

class AddReturn(StatesGroup):
    date = State()
    amount = State()

# --- Обработчики команд ---
bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start(message: types.Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if user:
        await message.answer(f"Добро пожаловать, {user['username']}! Ваша роль: {user['role']}\nИспользуйте /help для списка команд.")
    else:
        await message.answer("Добро пожаловать! Для начала работы укажите ваш логин из веб-приложения.\n/set_username <ваш_логин>")

@dp.message(Command("set_username"))
async def set_username(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Пожалуйста, укажите имя пользователя после команды, например: /set_username admin")
        return
    username = args[1]
    user = get_user_by_username(username)
    if not user:
        await message.answer("Пользователь с таким именем не найден. Зарегистрируйтесь сначала в веб-приложении.")
        return
    update_telegram_id(user['id'], message.from_user.id)
    await message.answer(f"Имя пользователя {username} успешно привязано к вашему Telegram аккаунту!")

@dp.message(Command("help"))
async def help_command(message: types.Message):
    text = """
    Доступные команды:
    /start - Начать работу
    /set_username <логин> - Привязать Telegram к пользователю
    /add_shift - Добавить новую смену
    /add_return - Добавить возврат к существующей смене
    /my_shifts - Показать мои смены за текущий месяц
    /all_shifts - (админ) Показать все смены
    /itogi - Показать итоги за месяц
    /help - Это сообщение
    """
    await message.answer(text)

@dp.message(Command("my_shifts"))
async def my_shifts(message: types.Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала привяжите аккаунт командой /set_username")
        return
    rows = get_shifts_for_user(user['id'])
    if not rows:
        await message.answer("У вас пока нет смен за этот месяц.")
        return
    text = "Ваши смены:\n\n"
    for row in rows:
        text += f"{row[1]} {row[2]} {row[3]} - ТО брутто: {row[5]} ₽, Настройки: {row[8]} ₽, Возвраты: {row[9]} ₽\n"
    await message.answer(text)

@dp.message(Command("all_shifts"))
async def all_shifts(message: types.Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user or user['role'] != 'admin':
        await message.answer("У вас нет прав для просмотра всех смен.")
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT * FROM smeny ORDER BY date, point, employee')
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        await message.answer("Смен пока нет.")
        return
    text = "Все смены:\n\n"
    for row in rows:
        salary = calculate_salary_row(row, *get_coeffs(user_id=None), max_mode=False)
        text += f"{row[1]} {row[2]} {row[3]} - ТО брутто: {row[5]} ₽, Возвраты: {row[9]} ₽, ЗП факт: {salary} ₽\n"
    await message.answer(text)

@dp.message(Command("itogi"))
async def itogi(message: types.Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала привяжите аккаунт командой /set_username")
        return
    if user['role'] == 'admin':
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
            total = sum(calculate_salary_row(row, *get_coeffs(user_id=None), max_mode=False) for row in rows)
            text += f"{emp}: {total} ₽\n"
        await message.answer(text)
    else:
        rows = get_shifts_for_user(user['id'])
        if not rows:
            await message.answer("У вас пока нет смен.")
            return
        total = sum(calculate_salary_row(row, *get_coeffs(user_id=user['id']), max_mode=False) for row in rows)
        await message.answer(f"Ваша итоговая зарплата за месяц: {total} ₽")

# --- Добавление смены (FSM) ---
@dp.message(Command("add_shift"))
async def add_shift_start(message: types.Message, state: FSMContext):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала привяжите аккаунт командой /set_username")
        return
    await state.update_data(user_id=user['id'], employee_name=user['username'])
    await message.answer("Введите дату смены (ГГГГ-ММ-ДД):")
    await state.set_state(AddShift.date)

@dp.message(AddShift.date)
async def shift_date(message: types.Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        await message.answer("Неверный формат. Введите дату ГГГГ-ММ-ДД:")
        return
    await state.update_data(date=date_str)
    # Выбор точки – инлайн-кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=point, callback_data=f"point_{point}")] for point in POINTS
    ])
    await message.answer("Выберите точку:", reply_markup=keyboard)
    await state.set_state(AddShift.point)

@dp.callback_query(StateFilter(AddShift.point))
async def shift_point(callback: types.CallbackQuery, state: FSMContext):
    point = callback.data.split('_')[1]
    await state.update_data(point=point)
    await callback.message.edit_text(f"Выбрана точка: {point}\nТеперь введите сотрудника (или оставьте пустым для использования вашего логина):")
    await callback.answer()
    await state.set_state(AddShift.employee)

@dp.message(AddShift.employee)
async def shift_employee(message: types.Message, state: FSMContext):
    employee = message.text.strip()
    if not employee:
        data = await state.get_data()
        employee = data.get('employee_name', 'Неизвестно')
    await state.update_data(employee=employee)
    await message.answer("Введите количество сотрудников в смене (число):")
    await state.set_state(AddShift.kol_vo)

@dp.message(AddShift.kol_vo)
async def shift_kolvo(message: types.Message, state: FSMContext):
    try:
        kol = int(message.text.strip())
        if kol <= 0:
            raise ValueError
        await state.update_data(kol_vo=kol)
    except ValueError:
        await message.answer("Введите положительное целое число:")
        return
    await message.answer("Введите ТО брутто (сумма продаж, ₽):")
    await state.set_state(AddShift.to_brutto)

@dp.message(AddShift.to_brutto)
async def shift_to(message: types.Message, state: FSMContext):
    try:
        to = float(message.text.strip())
        if to < 0:
            raise ValueError
        await state.update_data(to_brutto=to)
    except ValueError:
        await message.answer("Введите число >= 0:")
        return
    await message.answer("Введите сумму дорогостоя (₽) (если нет, введите 0):")
    await state.set_state(AddShift.summa_dorog)

@dp.message(AddShift.summa_dorog)
async def shift_summa_dorog(message: types.Message, state: FSMContext):
    try:
        summa = float(message.text.strip())
        if summa < 0:
            raise ValueError
        await state.update_data(summa_dorog=summa)
    except ValueError:
        await message.answer("Введите число >= 0:")
        return
    await message.answer("Введите количество дорогостоящих товаров (шт) (если нет, 0):")
    await state.set_state(AddShift.kol_vo_dorog)

@dp.message(AddShift.kol_vo_dorog)
async def shift_kolvo_dorog(message: types.Message, state: FSMContext):
    try:
        kol = int(message.text.strip())
        if kol < 0:
            raise ValueError
        await state.update_data(kol_vo_dorog=kol)
    except ValueError:
        await message.answer("Введите целое число >= 0:")
        return
    await message.answer("Введите сумму настроек за день (₽):")
    await state.set_state(AddShift.nastroiki)

@dp.message(AddShift.nastroiki)
async def shift_nastroiki(message: types.Message, state: FSMContext):
    try:
        nastroiki = float(message.text.strip())
        if nastroiki < 0:
            raise ValueError
        await state.update_data(nastroiki=nastroiki)
    except ValueError:
        await message.answer("Введите число >= 0:")
        return
    await message.answer("Введите сумму возвратов за день (₽) (если нет, 0):")
    await state.set_state(AddShift.returns)

@dp.message(AddShift.returns)
async def shift_returns(message: types.Message, state: FSMContext):
    try:
        returns = float(message.text.strip())
        if returns < 0:
            raise ValueError
        await state.update_data(returns=returns)
    except ValueError:
        await message.answer("Введите число >= 0:")
        return
    data = await state.get_data()
    user_id = data['user_id']
    date = data['date']
    point = data['point']
    employee = data['employee']
    kol_vo_sotr = data['kol_vo']
    to_brutto = data['to_brutto']
    summa_dorog = data['summa_dorog']
    kol_vo_dorog = data['kol_vo_dorog']
    nastroiki = data['nastroiki']
    returns_val = data['returns']

    existing = get_shift_by_date_point(user_id, date, point, employee)
    if existing:
        await message.answer("Смена за эту дату, точку и сотрудника уже существует. Удалите старую через веб-интерфейс.")
        await state.clear()
        return

    add_shift(user_id, date, point, employee, kol_vo_sotr, to_brutto, summa_dorog, kol_vo_dorog, nastroiki, returns_val)
    await message.answer(f"✅ Смена добавлена!\nДата: {date}\nТочка: {point}\nСотрудник: {employee}\nТО брутто: {to_brutto} ₽\nНастройки: {nastroiki} ₽\nВозвраты: {returns_val} ₽")
    await state.clear()

# --- Добавление возврата ---
@dp.message(Command("add_return"))
async def add_return_start(message: types.Message, state: FSMContext):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала привяжите аккаунт командой /set_username")
        return
    await state.update_data(user_id=user['id'])
    await message.answer("Введите дату смены (ГГГГ-ММ-ДД):")
    await state.set_state(AddReturn.date)

@dp.message(AddReturn.date)
async def return_date(message: types.Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        await message.answer("Неверный формат. Введите дату ГГГГ-ММ-ДД:")
        return
    await state.update_data(date=date_str)
    user_id = (await state.get_data())['user_id']
    rows = get_shifts_for_user(user_id, date_str)
    if not rows:
        await message.answer("Смен за эту дату не найдено. Сначала добавьте смену через /add_shift.")
        await state.clear()
        return
    if len(rows) == 1:
        await state.update_data(smena_id=rows[0][0])
        await message.answer(f"Найдена смена: {rows[0][1]} {rows[0][2]} {rows[0][3]}. Текущие возвраты: {rows[0][9]} ₽. Введите сумму возврата для добавления:")
        await state.set_state(AddReturn.amount)
    else:
        # Несколько смен – уточняем
        options = []
        for row in rows:
            options.append(f"{row[2]} - {row[3]} (возвраты: {row[9]})")
        await message.answer("Найдено несколько смен за эту дату. Пока выберите через веб-интерфейс.")
        await state.clear()

@dp.message(AddReturn.amount)
async def return_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число:")
        return
    data = await state.get_data()
    smena_id = data['smena_id']
    # Получаем текущие возвраты
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT returns FROM smeny WHERE id = %s', (smena_id,))
    current = cur.fetchone()[0] or 0.0
    new_returns = current + amount
    update_shift_returns(smena_id, new_returns)
    cur.close()
    conn.close()
    await message.answer(f"✅ Возврат добавлен! Новые общие возвраты за эту смену: {new_returns} ₽.")
    await state.clear()

# --- Запуск бота ---
async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
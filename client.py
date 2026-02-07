import asyncio
import json
import threading
import queue
import time
import tkinter as tk
from tkinter import simpledialog, messagebox, scrolledtext, ttk
import websockets

# --- Настройки ---
SERVER_URL = "ws://localhost:8765"

# --- Потокобезопасные очереди для обмена между GUI и ws-потоком ---
gui_in = queue.Queue()
ws_send_queue_holder = {'queue': None, 'loop': None}

# --- Глобальные переменные ---
global_root = None
chat_buttons = []  # Список кнопок чата для управления состоянием


# --- Помощники ---
def json_msg(obj):
    return json.dumps(obj, ensure_ascii=False)


def start_ws_thread():
    """Запуск отдельного потока с asyncio-loop и websocket-клиентом."""
    t = threading.Thread(target=ws_thread_entry, daemon=True)
    t.start()
    return t


def ws_thread_entry():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ws_send_q = asyncio.Queue()
    ws_send_queue_holder['queue'] = ws_send_q
    ws_send_queue_holder['loop'] = loop
    try:
        loop.run_until_complete(ws_main(SERVER_URL, ws_send_q))
    except Exception as e:
        gui_in.put({'action': 'sys', 'message': f'WS thread stopped: {e}'})
    finally:
        loop.close()


async def ws_main(url, send_q: asyncio.Queue):
    """Главная корутина в ws-потоке: соединение, recv и send loops."""
    reconnect_delay = 1
    while True:
        try:
            async with websockets.connect(url) as ws:
                gui_in.put({'action': 'sys', 'message': 'Connected to server'})
                consumer_task = asyncio.create_task(ws_consumer(ws))
                producer_task = asyncio.create_task(ws_producer(ws, send_q))
                done, pending = await asyncio.wait([consumer_task, producer_task], return_when=asyncio.FIRST_EXCEPTION)
                for p in pending:
                    p.cancel()
        except Exception as e:
            gui_in.put({'action': 'sys', 'message': f'Connection error: {e}. Reconnect in {reconnect_delay}s...'})
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 10)


async def ws_consumer(ws):
    """Чтение входящих сообщений с сервера и передача их GUI через очередь."""
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except Exception:
            msg = {'action': 'raw', 'data': raw}
        gui_in.put(msg)


async def ws_producer(ws, send_q: asyncio.Queue):
    """Отправляет команды, которые GUI положил в send_q."""
    while True:
        obj = await send_q.get()
        try:
            await ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception:
            gui_in.put({'action': 'sys', 'message': 'Failed to send message (ws)'})


# --- Интерфейс: состояние GUI (в функциональном стиле) ---
state = {
    'joined_rooms': {},  # room_id -> {'name': str, 'messages': [(ts, text)]}
    'private_chats': {},  # recipient -> {'messages': [(ts, text)], 'unread': int}
    'current_room': None,  # room_id или None
    'current_private_chat': None,  # recipient username или None
    'username': None,
    'authorized': False,
}


# --- Функции для взаимодействия GUI -> ws-thread ---
def send_to_server(obj):
    """Безопасно добавить задачу в asyncio.Queue в ws-потоке."""
    q = ws_send_queue_holder.get('queue')
    loop = ws_send_queue_holder.get('loop')
    if q is None or loop is None:
        messagebox.showerror("Not connected", "WebSocket not connected yet.")
        return
    asyncio.run_coroutine_threadsafe(q.put(obj), loop)


# --- Функция для включения/выключения элементов управления ---
def enable_chat_controls(enable: bool):
    """Включает или выключает элементы управления чата."""
    for btn in chat_buttons:
        btn.config(state=tk.NORMAL if enable else tk.DISABLED)


# --- Функция для ввода имени (ИСПРАВЛЕНО: повторные попытки при ошибке) ---
def prompt_username(initial=True):
    """Показывает диалог для ввода уникального имени с защитой от ошибок."""
    if initial:
        title = "Choose Username"
        prompt = "Please enter a unique username (3-20 characters):"
    else:
        title = "Username Error - Try Again"
        prompt = "Username invalid or taken. Please try another (3-20 characters):"

    username = simpledialog.askstring(title, prompt, parent=global_root)

    # Обработка нажатия Cancel
    if username is None:
        if initial:
            # Первый вызов + Cancel = запрос подтверждения выхода
            if messagebox.askyesno("Exit Application",
                                   "Do you really want to exit the application?",
                                   parent=global_root):
                global_root.quit()
            else:
                # Пользователь передумал — повторяем запрос
                global_root.after(100, lambda: prompt_username(initial=False))
        else:
            # Повторный вызов + Cancel = сразу выход
            global_root.quit()
        return

    username = username.strip()

    # Клиентская валидация (для быстрой обратной связи)
    if len(username) < 3:
        messagebox.showerror("Invalid Username",
                             "Username must be at least 3 characters long",
                             parent=global_root)
        global_root.after(100, lambda: prompt_username(initial=False))
        return

    if len(username) > 20:
        messagebox.showerror("Invalid Username",
                             "Username must be less than 20 characters",
                             parent=global_root)
        global_root.after(100, lambda: prompt_username(initial=False))
        return

    # Отправляем на сервер для проверки уникальности
    send_to_server({'action': 'set_username', 'username': username})


# --- GUI: обработчики и обновление UI ---
def gui_process_incoming(root, messages_text, rooms_listbox, my_rooms_listbox, entry_name, system_text):
    """Периодически проверяет gui_in и обновляет виджеты."""
    try:
        while True:
            msg = gui_in.get_nowait()
            process_server_message(msg, messages_text, rooms_listbox, my_rooms_listbox, entry_name, system_text)
    except queue.Empty:
        pass
    root.after(100, gui_process_incoming, root, messages_text, rooms_listbox, my_rooms_listbox, entry_name, system_text)


def process_server_message(msg, messages_text, rooms_listbox, my_rooms_listbox, entry_name, system_text):
    action = msg.get('action')
    if action == 'welcome':
        messages_text.insert(tk.END, f"[server] {msg.get('message')}\n", 'server')
        messages_text.tag_config('server', foreground='blue')
        messages_text.see(tk.END)
        # Запрашиваем имя ТОЛЬКО если ещё не авторизованы
        if not state['authorized'] and state['username'] is None:
            prompt_username(initial=True)

    elif action == 'username_set':
        username = msg.get('username')
        state['username'] = username
        state['authorized'] = True
        entry_name.config(state=tk.NORMAL)
        entry_name.delete(0, tk.END)
        entry_name.insert(0, username)
        entry_name.config(state=tk.DISABLED)
        system_text.insert(tk.END, f"✓ Authorized as: {username}\n")
        system_text.see(tk.END)
        enable_chat_controls(True)
        messagebox.showinfo("Authorized", msg.get('message', f'Welcome, {username}!'), parent=global_root)

    elif action == 'sys':
        system_text.insert(tk.END, f"{msg.get('message')}\n")
        system_text.see(tk.END)
    elif action == 'rooms_list':
        rooms_listbox.delete(0, tk.END)
        for r in msg.get('rooms', []):
            rooms_listbox.insert(tk.END, f"{r['id']} — {r['name']} ({r['members']})")
    elif action == 'room_created':
        r = msg.get('room')
        room_created_window = tk.Toplevel(global_root)
        room_created_window.title("Room created")
        room_created_window.geometry("400x200")
        tk.Label(room_created_window, text="Room created successfully!", font=('Arial', 12, 'bold')).pack(pady=10)
        tk.Label(room_created_window, text="Room ID:").pack(anchor=tk.W, padx=10)
        room_id_entry = tk.Entry(room_created_window, width=40)
        room_id_entry.pack(padx=10, pady=5)
        room_id_entry.insert(0, r['id'])
        room_id_entry.config(state=tk.DISABLED)
        tk.Label(room_created_window, text="Room name:").pack(anchor=tk.W, padx=10)
        tk.Label(room_created_window, text=r['name']).pack(padx=10, pady=5)
        button_frame = tk.Frame(room_created_window)
        button_frame.pack(pady=10)

        def copy_to_clipboard_local():
            room_id = room_id_entry.get()
            global_root.clipboard_clear()
            global_root.clipboard_append(room_id)
            messagebox.showinfo("Copied", f"Room ID '{room_id}' copied to clipboard!", parent=room_created_window)

        tk.Button(button_frame, text="Copy ID", command=copy_to_clipboard_local).pack(side=tk.LEFT, padx=5)
        tk.Button(button_frame, text="OK", command=room_created_window.destroy).pack(side=tk.LEFT, padx=5)
        send_to_server({'action': 'list_rooms'})
    elif action == 'joined':
        r = msg.get('room')
        room_id = r['id']
        state['joined_rooms'].setdefault(room_id, {'name': r['name'], 'messages': []})
        # Добавляем комнату в список "моих комнат"
        my_rooms_listbox.insert(tk.END, f"ROOM:{room_id} — {r['name']}")
        messages_text.insert(tk.END, f"[joined] {r['name']} ({room_id})\n", 'server')
        messages_text.tag_config('server', foreground='blue')
        messages_text.see(tk.END)
    elif action == 'user_joined':
        rid = msg.get('room_id')
        user = msg.get('user') or 'Anon'
        append_room_message(rid, f"[join] {user}", messages_text)
    elif action == 'user_left':
        rid = msg.get('room_id')
        user = msg.get('user') or 'Anon'
        append_room_message(rid, f"[left] {user}", messages_text)
    elif action == 'message':
        rid = msg.get('room_id')
        frm = msg.get('from') or 'Anon'
        text = msg.get('text') or ''
        if state['current_room'] == rid:
            append_room_message(rid, f"{frm}: {text}", messages_text)
    # --- Приватные сообщения ---
    elif action == 'private_message':
        frm = msg.get('from') or 'Anon'
        text = msg.get('text') or ''

        # Сохраняем сообщение в историю приватного чата
        state['private_chats'].setdefault(frm, {'messages': [], 'unread': 0})
        state['private_chats'][frm]['messages'].append((time.time(), f"{frm}: {text}"))
        state['private_chats'][frm]['unread'] += 1

        # Если чат ещё не в списке "моих комнат" — добавляем его
        chat_exists = False
        for i in range(my_rooms_listbox.size()):
            item = my_rooms_listbox.get(i)
            if item.startswith(f"PM:{frm}"):
                chat_exists = True
                # Обновляем счётчик непрочитанных
                unread = state['private_chats'][frm]['unread']
                my_rooms_listbox.delete(i)
                my_rooms_listbox.insert(i, f"PM:{frm} ({unread} new)")
                break

        if not chat_exists:
            my_rooms_listbox.insert(tk.END, f"PM:{frm} (1 new)")

        # Если это текущий приватный чат — отображаем сообщение
        if state['current_private_chat'] == frm:
            messages_text.insert(tk.END, f"{frm}: {text}\n", 'private')
            messages_text.tag_config('private', foreground='purple')
            messages_text.see(tk.END)
            # Сбрасываем счётчик непрочитанных
            state['private_chats'][frm]['unread'] = 0

        # Уведомление в системных сообщениях
        system_text.insert(tk.END, f"[PM from {frm}] {text}\n")
        system_text.see(tk.END)

    elif action == 'private_message_sent':
        to = msg.get('to') or 'unknown'
        text = msg.get('text') or ''

        # Сохраняем исходящее сообщение
        state['private_chats'].setdefault(to, {'messages': [], 'unread': 0})
        state['private_chats'][to]['messages'].append((time.time(), f"You: {text}"))

        # Если это текущий приватный чат — отображаем сообщение
        if state['current_private_chat'] == to:
            messages_text.insert(tk.END, f"You: {text}\n", 'private_sent')
            messages_text.tag_config('private_sent', foreground='darkgreen')
            messages_text.see(tk.END)

        # Уведомление в системных сообщениях
        system_text.insert(tk.END, f"[PM to {to}] {text}\n")
        system_text.see(tk.END)

    elif action == 'error':
        # Специальная обработка ошибок авторизации
        if not state['authorized']:
            error_msg = msg.get('message', 'Authorization failed')
            messagebox.showerror("Authorization Error", error_msg, parent=global_root)
            # Даём пользователю возможность повторить попытку
            global_root.after(100, lambda: prompt_username(initial=False))
        else:
            # Обычные ошибки для авторизованных пользователей
            messages_text.insert(tk.END, f"[error] {msg.get('message')}\n", 'error')
            messages_text.tag_config('error', foreground='red')
            messages_text.see(tk.END)
    else:
        messages_text.insert(tk.END, f"[raw] {msg}\n")
        messages_text.see(tk.END)


def append_room_message(room_id, text, messages_text):
    state['joined_rooms'].setdefault(room_id, {'name': room_id, 'messages': []})
    state['joined_rooms'][room_id]['messages'].append((time.time(), text))
    if state['current_room'] == room_id:
        messages_text.insert(tk.END, text + "\n")
        messages_text.see(tk.END)


# --- GUI: кнопки ---
def on_refresh_rooms():
    if not state['authorized']:
        messagebox.showinfo("Not Authorized",
                            "Please choose a username first!\nClick 'Cancel' in the username dialog to exit the application.",
                            parent=global_root)
        prompt_username(initial=False)
        return
    send_to_server({'action': 'list_rooms'})


def on_create_room(root):
    if not state['authorized']:
        messagebox.showinfo("Not Authorized",
                            "Please choose a username first!\nClick 'Cancel' in the username dialog to exit the application.",
                            parent=global_root)
        prompt_username(initial=False)
        return
    name = simpledialog.askstring("Create room", "Room name:", parent=root)
    if name:
        send_to_server({'action': 'create_room', 'name': name})


def on_select_room_and_join(rooms_listbox, entry_name):
    if not state['authorized']:
        messagebox.showinfo("Not Authorized",
                            "Please choose a username first!\nClick 'Cancel' in the username dialog to exit the application.",
                            parent=global_root)
        prompt_username(initial=False)
        return
    sel = rooms_listbox.curselection()
    if not sel:
        messagebox.showinfo("Select", "Select a room from the list", parent=global_root)
        return
    text = rooms_listbox.get(sel[0])
    room_id = text.split()[0]
    display_name = state['username']
    send_to_server({'action': 'join', 'room_id': room_id, 'display_name': display_name})
    state['current_room'] = room_id
    state['current_private_chat'] = None  # Сбрасываем приватный чат


def on_join_by_id(room_id_entry, entry_name):
    if not state['authorized']:
        messagebox.showinfo("Not Authorized",
                            "Please choose a username first!\nClick 'Cancel' in the username dialog to exit the application.",
                            parent=global_root)
        prompt_username(initial=False)
        return
    room_id = room_id_entry.get().strip()
    if not room_id:
        messagebox.showinfo("Input", "Enter room id", parent=global_root)
        return
    display_name = state['username']
    send_to_server({'action': 'join', 'room_id': room_id, 'display_name': display_name})
    state['current_room'] = room_id
    state['current_private_chat'] = None  # Сбрасываем приватный чат


def on_my_room_select(evt, messages_text):
    if not state['authorized']:
        return
    widget = evt.widget
    sel = widget.curselection()
    if not sel:
        return
    text = widget.get(sel[0])

    # Определяем тип чата по префиксу
    if text.startswith("ROOM:"):
        # Обычная комната
        room_id = text.split(":")[1].split()[0]
        state['current_room'] = room_id
        state['current_private_chat'] = None

        # Отображаем историю комнаты
        messages_text.delete(1.0, tk.END)
        for _, msg in state['joined_rooms'].get(room_id, {}).get('messages', []):
            messages_text.insert(tk.END, msg + "\n")
        messages_text.see(tk.END)

    elif text.startswith("PM:"):
        # Приватный чат
        recipient = text.split(":")[1].split()[0]
        state['current_room'] = None
        state['current_private_chat'] = recipient

        # Отображаем историю приватного чата
        messages_text.delete(1.0, tk.END)
        for _, msg in state['private_chats'].get(recipient, {}).get('messages', []):
            messages_text.insert(tk.END, msg + "\n")
            # Применяем стили
            if msg.startswith("You:"):
                messages_text.tag_add('private_sent', 'end-2c linestart', 'end-1c')
                messages_text.tag_config('private_sent', foreground='darkgreen')
            else:
                messages_text.tag_add('private', 'end-2c linestart', 'end-1c')
                messages_text.tag_config('private', foreground='purple')
        messages_text.see(tk.END)

        # Сбрасываем счётчик непрочитанных
        if recipient in state['private_chats']:
            state['private_chats'][recipient]['unread'] = 0
            # Обновляем запись в списке
            idx = widget.curselection()[0]
            widget.delete(idx)
            widget.insert(idx, f"PM:{recipient}")


def on_send_message(message_text, messages_text):
    if not state['authorized']:
        messagebox.showinfo("Not Authorized",
                            "Please choose a username first!\nClick 'Cancel' in the username dialog to exit the application.",
                            parent=global_root)
        prompt_username(initial=False)
        return
    text = message_text.get("1.0", tk.END).strip()
    if not text:
        return

    # Определяем, куда отправлять: в комнату или приватный чат
    if state['current_private_chat']:
        # Отправляем приватное сообщение
        recipient = state['current_private_chat']
        send_to_server({'action': 'private_message', 'to': recipient, 'text': text})
        # Очищаем поле ввода — отображение произойдёт при получении private_message_sent
        message_text.delete("1.0", tk.END)
        return

    elif state['current_room']:
        # Отправляем в комнату
        room_id = state['current_room']
        send_to_server({'action': 'message', 'room_id': room_id, 'text': text})
        message_text.delete("1.0", tk.END)
        return

    else:
        messagebox.showinfo("No chat", "Select a room or private chat first", parent=global_root)
        return


def on_leave_room(messages_text):
    if not state['authorized']:
        return
    if state['current_room']:
        room_id = state['current_room']
        send_to_server({'action': 'leave', 'room_id': room_id})
        state['current_room'] = None
        messages_text.delete(1.0, tk.END)
        # Удаляем комнату из списка "моих комнат"
        for i in range(my_rooms_listbox.size()):
            if my_rooms_listbox.get(i).startswith(f"ROOM:{room_id}"):
                my_rooms_listbox.delete(i)
                break
        messagebox.showinfo("Left room", f"You left room {room_id}", parent=global_root)
    elif state['current_private_chat']:
        # Просто закрываем приватный чат (не удаляем его из списка)
        state['current_private_chat'] = None
        messages_text.delete(1.0, tk.END)
        messagebox.showinfo("Chat closed", f"Private chat closed", parent=global_root)
    else:
        messagebox.showinfo("No chat", "You are not in any chat", parent=global_root)


# --- Функция для открытия приватного чата ---
def on_open_private_chat():
    """Открывает приватный чат с пользователем (добавляет в список 'моих комнат')."""
    if not state['authorized']:
        messagebox.showinfo("Not Authorized",
                            "Please choose a username first!\nClick 'Cancel' in the username dialog to exit the application.",
                            parent=global_root)
        prompt_username(initial=False)
        return

    recipient = simpledialog.askstring("Private message", "Recipient name:", parent=global_root)
    if not recipient:
        return

    # Добавляем чат в список "моих комнат", если его ещё нет
    chat_exists = False
    for i in range(my_rooms_listbox.size()):
        item = my_rooms_listbox.get(i)
        if item.startswith(f"PM:{recipient}"):
            chat_exists = True
            break

    if not chat_exists:
        my_rooms_listbox.insert(tk.END, f"PM:{recipient}")

    # Автоматически выбираем этот чат
    for i in range(my_rooms_listbox.size()):
        if my_rooms_listbox.get(i).startswith(f"PM:{recipient}"):
            my_rooms_listbox.selection_clear(0, tk.END)
            my_rooms_listbox.selection_set(i)
            on_my_room_select(type('event', (), {'widget': my_rooms_listbox})(), messages_text)
            break


# --- Построение GUI ---
def build_gui():
    global global_root, my_rooms_listbox, messages_text
    root = tk.Tk()
    global_root = root
    root.title("Messenger XAM")
    root.geometry("1000x700")
    root.minsize(800, 500)

    style = ttk.Style()
    style.theme_use('clam')
    style.configure('TFrame', background='#f0f0f0')
    style.configure('TButton', background='#4a90e2', foreground='white', padding=5)
    style.configure('TLabel', background='#f0f0f0', font=('Arial', 10))
    style.configure('TEntry', fieldbackground='#ffffff', background='#ffffff')

    top = ttk.Frame(root)
    top.pack(fill=tk.X, padx=10, pady=5)
    ttk.Label(top, text="Display name:").pack(side=tk.LEFT, padx=5)
    entry_name = ttk.Entry(top, width=20)
    entry_name.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
    entry_name.insert(0, "Waiting for authorization...")
    entry_name.config(state=tk.DISABLED)

    main_frame = ttk.Frame(root)
    main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

    left_panel = ttk.Frame(main_frame, width=250)
    left_panel.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

    center_panel = ttk.Frame(main_frame)
    center_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    right_panel = ttk.Frame(main_frame, width=300)
    right_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))

    ttk.Label(left_panel, text="Available rooms:", font=('Arial', 10, 'bold')).pack(anchor=tk.W, pady=(0, 5))
    rooms_listbox = tk.Listbox(left_panel, height=10, width=40, font=('Arial', 10))
    rooms_listbox.pack(fill=tk.X, expand=True, pady=(0, 10))

    btn_frame = ttk.Frame(left_panel)
    btn_frame.pack(fill=tk.X)
    btn_refresh = ttk.Button(btn_frame, text="Refresh", command=on_refresh_rooms)
    btn_refresh.pack(side=tk.LEFT, padx=5, pady=5)
    chat_buttons.append(btn_refresh)
    btn_create = ttk.Button(btn_frame, text="Create", command=lambda: on_create_room(root))
    btn_create.pack(side=tk.LEFT, padx=5, pady=5)
    chat_buttons.append(btn_create)
    btn_join = ttk.Button(btn_frame, text="Join selected",
                          command=lambda: on_select_room_and_join(rooms_listbox, entry_name))
    btn_join.pack(side=tk.LEFT, padx=5, pady=5)
    chat_buttons.append(btn_join)

    ttk.Label(left_panel, text="Join by id:", font=('Arial', 10, 'bold')).pack(anchor=tk.W, pady=(10, 5))
    id_entry = ttk.Entry(left_panel, width=30)
    id_entry.pack(fill=tk.X, pady=5)
    btn_join_id = ttk.Button(left_panel, text="Join by id", command=lambda: on_join_by_id(id_entry, entry_name))
    btn_join_id.pack(fill=tk.X, pady=5)
    chat_buttons.append(btn_join_id)

    ttk.Label(left_panel, text="My rooms (session):", font=('Arial', 10, 'bold')).pack(anchor=tk.W, pady=(15, 5))
    my_rooms_listbox = tk.Listbox(left_panel, height=12, width=40, font=('Arial', 10))
    my_rooms_listbox.pack(fill=tk.X, expand=True)
    my_rooms_listbox.bind('<<ListboxSelect>>', lambda evt: on_my_room_select(evt, messages_text))

    # Кнопка для приватных чатов
    btn_private = ttk.Button(left_panel, text="Start Private Chat", command=on_open_private_chat)
    btn_private.pack(fill=tk.X, pady=(10, 0))
    chat_buttons.append(btn_private)

    ttk.Label(center_panel, text="Messages:", font=('Arial', 10, 'bold')).pack(anchor=tk.W, pady=(0, 5))
    messages_text = scrolledtext.ScrolledText(center_panel, state=tk.NORMAL, font=('Arial', 10), wrap=tk.WORD)
    messages_text.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

    bottom_frame = ttk.Frame(center_panel)
    bottom_frame.pack(fill=tk.X)
    message_text = tk.Text(bottom_frame, height=3, wrap=tk.WORD, font=('Arial', 10))
    message_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
    btn_send = ttk.Button(bottom_frame, text="Send", command=lambda: on_send_message(message_text, messages_text))
    btn_send.pack(side=tk.LEFT, padx=5)
    chat_buttons.append(btn_send)
    btn_leave = ttk.Button(bottom_frame, text="Leave/Close Chat", command=lambda: on_leave_room(messages_text))
    btn_leave.pack(side=tk.LEFT, padx=5)
    chat_buttons.append(btn_leave)

    ttk.Label(right_panel, text="System messages:", font=('Arial', 10, 'bold')).pack(anchor=tk.W, pady=(0, 5))
    system_text = scrolledtext.ScrolledText(right_panel, state=tk.NORMAL, height=5, font=('Arial', 9))
    system_text.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

    enable_chat_controls(False)
    start_ws_thread()
    root.after(100, gui_process_incoming, root, messages_text, rooms_listbox, my_rooms_listbox, entry_name, system_text)
    message_text.focus()
    messages_text.tag_config('server', foreground='blue')
    messages_text.tag_config('error', foreground='red')
    messages_text.tag_config('private', foreground='purple')
    messages_text.tag_config('private_sent', foreground='darkgreen')

    return root


if __name__ == "__main__":
    root = build_gui()
    root.mainloop()
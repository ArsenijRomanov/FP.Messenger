import asyncio
import json
import time
import argparse
import websockets

SERVER_URL_DEFAULT = "ws://localhost:8765"


def j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


async def wait_for_action(ws, action: str, timeout: float = 5.0):
    """Ждём конкретное action-сообщение от сервера (игнорируем остальное)."""
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            raise TimeoutError(f"Timeout waiting for action={action}")
        raw = await asyncio.wait_for(ws.recv(), timeout=left)
        msg = json.loads(raw)
        if msg.get("action") == action:
            return msg


async def receiver(ws, stats: dict, slow_read_s: float = 0.0):
    """Постоянно читает входящие сообщения, считает только action='message'."""
    try:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("action") == "message":
                stats["received"] += 1
            # искусственно замедляем чтение
            if slow_read_s > 0:
                await asyncio.sleep(slow_read_s)
    except Exception:
        # при разрыве соединения просто выходим
        return


async def virtual_user(
    idx: int,
    url: str,
    room_id_future: asyncio.Future,
    start_sending: asyncio.Event,
    messages_per_user: int,
    send_delay_s: float,
    slow_read_s: float,
    stats_out: dict,
):
    username = f"user{idx:02d}"

    async with websockets.connect(url) as ws:
        # сервер шлёт welcome — просто дождёмся, чтобы не слать раньше времени
        await wait_for_action(ws, "welcome", timeout=10)

        # ставим имя
        await ws.send(j({"action": "set_username", "username": username}))

        # ждём подтверждение
        while True:
            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("action") == "username_set":
                break
            if msg.get("action") == "error":
                # если вдруг имя занято — добавим суффикс и попробуем ещё раз
                username = f"{username}_{int(time.time()*1000)%10000}"
                await ws.send(j({"action": "set_username", "username": username}))

        # первый клиент создаёт комнату
        if idx == 0:
            await ws.send(j({"action": "create_room", "name": "Load Test Room"}))
            created = await wait_for_action(ws, "room_created", timeout=10)
            room_id = created["room"]["id"]
            room_id_future.set_result(room_id)

        # остальные ждут id комнаты
        room_id = await room_id_future

        await ws.send(j({"action": "join", "room_id": room_id, "display_name": username}))
        await wait_for_action(ws, "joined", timeout=10)

        # запускаем приёмник
        stats = {"sent": 0, "received": 0, "username": username}
        recv_task = asyncio.create_task(receiver(ws, stats, slow_read_s=slow_read_s))

        # ждём общий старт (чтобы все начали слать одновременно)
        await start_sending.wait()

        # шлём сообщения
        for i in range(messages_per_user):
            await ws.send(j({"action": "message", "room_id": room_id, "text": f"hi from {username} #{i}"}))
            stats["sent"] += 1
            if send_delay_s > 0:
                await asyncio.sleep(send_delay_s)

        # даём немного времени на доставку последней волны сообщений
        await asyncio.sleep(1.0)

        recv_task.cancel()
        stats_out[idx] = stats


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=SERVER_URL_DEFAULT)
    ap.add_argument("-n", "--clients", type=int, default=50)
    ap.add_argument("-m", "--messages", type=int, default=20)
    ap.add_argument("--delay", type=float, default=0.01, help="delay between sends (seconds)")
    ap.add_argument("--slow-count", type=int, default=0, help="how many clients are slow readers")
    ap.add_argument("--slow-read", type=float, default=0.2, help="sleep after each received frame (seconds)")
    args = ap.parse_args()

    room_id_future = asyncio.Future()
    start_sending = asyncio.Event()
    stats_out = {}

    t0 = time.time()

    tasks = []
    for i in range(args.clients):
        slow_read_s = args.slow_read if i < args.slow_count else 0.0
        tasks.append(asyncio.create_task(
            virtual_user(
                idx=i,
                url=args.url,
                room_id_future=room_id_future,
                start_sending=start_sending,
                messages_per_user=args.messages,
                send_delay_s=args.delay,
                slow_read_s=slow_read_s,
                stats_out=stats_out,
            )
        ))

    # ждём создания комнаты и подключения части клиентов
    await asyncio.sleep(1.0)
    start_sending.set()

    await asyncio.gather(*tasks, return_exceptions=True)

    dt = time.time() - t0

    total_sent = sum(s["sent"] for s in stats_out.values())
    total_recv = sum(s["received"] for s in stats_out.values())
    per_client_recv = [s["received"] for s in stats_out.values()]

    print("\n=== LOAD TEST RESULT ===")
    print(f"clients: {args.clients}")
    print(f"messages per client: {args.messages}")
    print(f"total sent: {total_sent}")
    print(f"total received (only action=message): {total_recv}")
    if per_client_recv:
        print(f"received per client: min={min(per_client_recv)} avg={sum(per_client_recv)/len(per_client_recv):.1f} max={max(per_client_recv)}")
    print(f"elapsed: {dt:.2f}s")
    if dt > 0:
        print(f"throughput (sent/sec): {total_sent/dt:.1f}")


if __name__ == "__main__":
    asyncio.run(main())

# Тесты для server2.py

## Установка зависимостей (dev)
```bash
python -m pip install -r requirements-dev.txt
```

## Запуск тестов
Из каталога, где лежат `server2.py` и папка `tests/`:
```bash
pytest
```

## Запуск с покрытием
```bash
pytest --cov=server2 --cov-report=term-missing
```

## Что покрывается
- Все handlers (set_username / create_room / list_rooms / join / leave / message / private_message)
- Room dispatcher (broadcast + ветка QueueFull + cancel cleanup)
- client_writer / unregister_client (через unit/e2e)
- ws_handler (welcome, invalid json, too large, unknown action, handler exception)
- E2E сценарии (2 клиента): комнаты, broadcast, приватные сообщения, уникальные username, cleanup по disconnect

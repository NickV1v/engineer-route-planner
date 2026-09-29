Выезд — планирование работы выездных инженеров

FastAPI/Python, оптимизатор C++20, React/TypeScript и Leaflet.
Загрузка заявок и инженеров, распределение работ, карта, перепланирование,
история принятых планов и экспорт результатов.

Локальный запуск: Python 3.12+, Node.js 22.12+, Make, компилятор C++20.

  python3.12 -m venv .venv
  source .venv/bin/activate
  python -m pip install -r requirements.lock
  python -m pip install -e . --no-deps
  make solver
  npm --prefix frontend ci
  npm --prefix frontend run build
  python -m uvicorn dispatch.api:app --host 127.0.0.1 --port 8000

Интерфейс: http://127.0.0.1:8000
Разработка интерфейса: npm --prefix frontend run dev

Исходные архивы и таблица нормативов не распространяются с кодом.
Для импорта необходим локальный файл Нормативы.xlsx в корне проекта;
для прежнего CLI и интеграционных тестов также нужен Обезличивание.zip.
Используются только разрешённые синтетические данные, не контрольные CSV.
Примеры в data/samples — синтетические, seed конфигурации: 20260917.
Файлы в data/samples/invalid намеренно содержат ошибки валидации.

Реальные маршруты требуют установки requirements-routing.lock, подготовки
дорожных и транспортных графов скриптами scripts/ и путей config/routing.json.
Эти графы и базы данных в репозиторий не включены. Без них реальный расчёт
не работает. DISPATCH_ROUTING_MODE=synthetic — только тестовый режим.
Время в пути модельное, без пробок и точных расписаний. Кеш используется
только в пределах одного расчёта; архивы сохраняют историю.

Необязательные подсказки адресов: переменная окружения DADATA_API_KEY.
Ключи задаются локально и не должны попадать в Git.

Проверки: make test; npm --prefix frontend run build;
после сборки: npm --prefix frontend run test:e2e.
Интеграционные проверки требуют указанных выше локальных входных файлов.

Зависимость solver/third_party/json.hpp: nlohmann/json v3.12.0,
https://github.com/nlohmann/json — лицензия в solver/third_party/LICENSE.json.

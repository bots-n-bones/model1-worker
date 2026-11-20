# ТЗ для сайта: модель `model_1` через внешнего исполнителя (worker)

Этот файл можно отдать агенту/разработчику сайта целиком. Источник генерации здесь
намеренно не назван: сайт знает только «model_1» и секретный ключ исполнителя.

## Суть

Сервер сайта сам никуда не ходит. Исполнитель (worker.py на ПК) каждые ~4 секунды
опрашивает сайт, забирает задачу, генерирует и загружает готовые картинки обратно.

```
Студия ──create──▶ backend ──кладёт задачу──▶ data/m1_jobs.json
Исполнитель ──m1_next──▶ backend (выдаёт задачу)
Исполнитель ──m1_complete (файлы)──▶ backend ──▶ галерея
Студия ──status──▶ backend ──▶ {done, urls}
```

`m1_reference.php` — проверенная эталонная реализация очереди и служебных действий
(PHP 8.x). Перенести в свой backend, подключив к своим задачам, кредитам, галерее.
Протокол (имена действий, заголовок, JSON) менять нельзя — исполнитель уже готов.

## 1. Секрет

В приватный конфиг сайта: `MODEL1_WORKER_KEY` = ключ из `worker_config.json`
(его создаёт `install.ps1` на Windows или `install.sh` на Mac). Сравнение только `hash_equals`. Нет ключа → 403.

## 2. Очередь (`m1_with_queue`, `m1_housekeeping`)

- Файл `data/m1_jobs.json` (закрыт от веба), доступ под `flock`.
- `data/m1_worker.json` — `last_seen`; исполнитель «в сети», если опрашивал ≤ 90 с назад
  (`m1_worker_online`). Если не в сети — отказ в `create` до списания кредитов.
- Задача `generate`: `id`, `owner`, `prompt`, `aspect_ratio`, `count` (1–4), `image_urls`
  (0–10 референсов), `resolution` (`1k`|`2k`), `status`
  (`queued`→`processing`→`done`|`failed`), `urls[]`, `resolutions[]`, `error_code`.
- В работе дольше `M1_STALE_SEC` (1200 с) → снова в очередь (до 2 попыток), иначе failed.
  Старше `M1_EXPIRE_SEC` (2700 с) → failed/timeout. Завершённые чистятся через 7 дней.
- **Любой перевод в failed — вернуть кредиты.**

## 3. Создание задачи (`action=create`, модель model_1)

Обычные проверки сайта (пользователь, фильтр, цена, удержание кредитов), затем
`m1_enqueue($taskId, $prompt, $aspect, $count, $owner, $image_urls, $resolution)`:

- `aspect_ratio`: `16:9`, `4:3`, `1:1`, `3:4`, `9:16`;
- `count` 1–4 — **одна задача на все картинки**, не разбивать на несколько create;
- `image_urls` — до 10 путей к файлам ЭТОГО сайта (`/gallery/...jpg|png|webp`);
  исполнитель скачивает их по HTTPS с того же домена. Локальные/blob-картинки
  студии сначала загрузить на сайт и передать путь. Одинаково для всех студий/редакторов;
- `resolution`: `1k` (по умолчанию) или `2k` — подключить к кнопке «Разрешение».
  2K добавляет 30–60 с на картинку.

## 4. Статус (`action=status`)

Из очереди (`m1_job`, `m1_position`): `queued`/`processing` → `{done:false, state, position}`;
`done` → `{done:true, url, urls}`; `failed` → текст по `error_code`:

| error_code | текст |
|---|---|
| content_blocked | Запрос отклонён фильтром модели |
| timeout | Превышено время ожидания |
| quota | Исчерпан лимит модели, попробуйте позже |
| worker_auth | Модель временно недоступна |
| bad_request | Некорректный запрос (например, файл-референс не принят) |
| refs_unsupported | Модель сейчас не принимает референсы |
| upscale_unavailable | 2K недоступно для этого изображения |
| generation_failed | Не удалось сгенерировать изображение |

Если пришло картинок меньше `count` — показать пришедшие и вернуть кредиты за недостающие.

## 5. Действия исполнителя (только POST, заголовок `X-Worker-Key`, без токена пользователя)

**`?action=m1_next`** — тело `{}`. Обновляет `last_seen`, выдаёт самую старую задачу:
```json
{"job": {"id": "…", "type": "generate", "prompt": "…", "aspect_ratio": "1:1",
         "count": 2, "image_urls": ["/gallery/RefAssets/a.jpg"], "resolution": "2k"}}
```
или `{"job": null}`. Должно отвечать быстро.

**`?action=m1_complete`** — multipart: `job_id`, `files[]` (JPEG/PNG до 20 МБ, лишние сверх
`count` игнорируются), `resolutions` (необязательно, `"2k,2k,1k"` — фактическое
разрешение каждого файла; `1k` = повышение не удалось, пришёл оригинал).
Проверки: `is_uploaded_file`, размер, `finfo`, `getimagesize`. Сохранить в галерею,
ответ `{"ok": true, "urls": [...]}`. Ошибки 400/404/409/413/415.

**`?action=m1_fail`** — JSON `{"job_id": "…", "error_code": "…"}` → failed + возврат кредитов.
Повторный вызов для завершённой задачи — тоже `{"ok": true}`.

Ограничения сервера: не блокировать POST с `X-Worker-Key` и User-Agent `m1-worker/1.0`;
`upload_max_filesize` ≥ 20M, `post_max_size` ≥ 80M.

## 6. Необязательно: 2K для уже готовой картинки

Эталон поддерживает задачу `type: "upscale"` (`m1_enqueue_upscale($source_id, $index, $owner)`;
исполнитель получает `{id, type:"upscale", source_id, index, scale:"2k"}` и сдаёт один файл
через `m1_complete`). Нужна, только если хотите кнопку «скачать в 2K» у готовых 1K-картинок.

## 7. Проверка без исполнителя (curl)

```bash
curl -s -X POST 'https://SITE/backend.php?action=m1_next' -H 'X-Worker-Key: KEY' -d '{}'
curl -s -X POST 'https://SITE/backend.php?action=m1_complete' -H 'X-Worker-Key: KEY' \
     -F job_id=ID -F resolutions=1k -F 'files[]=@test.jpg;type=image/jpeg'
curl -s -X POST 'https://SITE/backend.php?action=m1_fail' -H 'X-Worker-Key: KEY' \
     -H 'Content-Type: application/json' -d '{"job_id":"ID","error_code":"timeout"}'
# без ключа / с неверным ключом — 403
```

# clean-webos-tv

Агент (Claude Code, Codex, Gemini CLI) работает с настоящим LG webOS TV по сети: нажимает кнопки
пульта, снимает скриншоты, читает и меняет настройки и сам проверяет результат. Идея взята у
[CleanYourTV](https://github.com/Gathin23/cleanyourtv), который делает это на Android TV через adb.
На webOS adb нет, поэтому здесь другие каналы:

| Android TV (CleanYourTV) | LG webOS (здесь) |
|---|---|
| adb по сети, порт 5555 | сетевой API ТВ (SSAP, `wss://TV:3001`): `tools/lgtv.py` |
| `pm disable-user` | настройки через сервис настроек + удаление приложений через меню пультом |
| `input keyevent` | `lgtv.py button` (как с пульта) |
| `screencap` | `lgtv.py screenshot` (весь экран) |
| `restore.sh` | `tools/restore.sh`: откат по журналу `changes.jsonl` + список ручных изменений |
| — | Developer Mode + `ares-*` + DevTools: цикл разработки своего приложения |

## Два режима

**1. Очистка ТВ** ([PROMPT.md](./PROMPT.md)). Выключить Live Plus и сбор данных о просмотре, рекламу
на Home и в заставке, удалить ненужные предустановленные приложения, по желанию DNS-блокировка.
Сначала резервная копия, потом небольшие партии изменений со скриншотом после каждой. Developer Mode
не нужен.

**2. Цикл разработки** ([PROMPT-DEV.md](./PROMPT-DEV.md)). ТВ в режиме разработчика: `ares-install`
и `ares-launch` ставят и запускают сборку, `ares-inspect` открывает Chrome DevTools прямо в
приложении. Через DevTools агент читает консоль и сетевые запросы (плейлисты и фрагменты hls.js),
смотрит DOM и диагностическую панель, делает скриншоты и выполняет JS. Кнопки пульта нажимаются
через сетевой API ТВ или key-событиями DevTools. Собрал, поставил на G5, сам проверил. Профиль для
[kinopub.webos](https://github.com/kaaburgh/kinopub.webos): [profiles/kinopub.webos.md](./profiles/kinopub.webos.md).

## Быстрый старт

```sh
git clone https://github.com/kaaburgh/clean-webos-tv && cd clean-webos-tv
python3 -m venv .venv && . .venv/bin/activate && pip install -r tools/requirements.txt
export TV_HOST=192.168.1.50
tools/lgtv.py pair            # примите запрос на ТВ
tools/lgtv.py info
tools/lgtv.py screenshot shots/home.jpg HOME
```

Дальше откройте агента в этой папке и вставьте нужный промпт.

## Инструменты

| Файл | Что делает | Зависимости |
|---|---|---|
| `tools/lgtv.py` | сопряжение, info, приложения, запуск/закрытие, кнопки, скриншот, чтение и запись настроек с журналом, откат, сырые SSAP/luna-запросы | `bscpylgtv` |
| `tools/backup.sh` | снимок ТВ в `backup/<дата>/` до изменений | `lgtv.py` |
| `tools/restore.sh` | откат настроек из журнала, список ручных изменений и удалённых приложений | `lgtv.py` |
| `tools/devloop.sh` | build → package → ares-install → ares-launch → ares-inspect | Node/yarn, ares-cli |
| `tools/inspect.sh` | держит `ares-inspect` в фоне, пишет адрес в `.devtools-endpoint` | ares-cli |
| `tools/devtools.py` | DevTools Protocol: eval, run, console, network, dom, screenshot, key, reload | только stdlib |

## Ограничения, о которых стоит знать

- Без root на webOS нельзя отключать системные службы, как `pm disable-user` на Android. Что можно
  выключить, выключается настройками; что можно удалить, удаляется как пользователем.
- Запись настроек идёт через известный приём с кнопкой уведомления (`luna://` в `createAlert`), как в
  `bscpylgtv`. Ответа нет, поэтому `lgtv.py` после записи читает значение обратно. LG может закрыть
  этот приём в новой прошивке; тогда остаётся меню и пульт.
- Скриншот через `tv/executeOneShot` не снимает защищённое видео и Live TV. DevTools-скриншот
  снимает только веб-слой, видео на нём чёрное.
- Пути меню и ключи настроек на разных прошивках разные. Инструменты проверяют, а не угадывают.
- Сетевые части проверены тестами против фейкового ТВ и `devtools.py` против настоящего Chromium.
  На живом LG G5 их ещё предстоит прогнать.

## Разработка

```sh
python -m unittest discover -s tests -v
shellcheck tools/*.sh
```

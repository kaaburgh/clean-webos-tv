# Профиль: kinopub.webos на LG G5

Приложение: [kaaburgh/kinopub.webos](https://github.com/kaaburgh/kinopub.webos), id `kinopub.webos`,
Enact + hls.js 1.0.10, ES5-сборка. Правила работы с репозиторием в его `AGENTS.md` и
`docs/agent-playbook.md` имеют приоритет над этим файлом.

## Сборка и установка

```sh
cd kinopub.webos
yarn install --frozen-lockfile     # ставит и локальные ares-* (@webosose/ares-cli)
ARES_DEVICE=lg-g5 ../clean-webos-tv/tools/devloop.sh
```

Пакет: `out/kinopub.webos_v<version>.ipk`. `devloop.sh` берёт самый свежий `.ipk` из `out/`.
Скрипты предпочитают локальные `ares-*` из `node_modules/.bin` (это `@webosose/ares-cli`). Если
`ares-inspect` оттуда не печатает адрес DevTools для ТВ, поставьте TV-версию CLI
(`npm i -g @webos-tools/cli`) и запускайте с `ARES_GLOBAL=1`.

## Пульт в приложении

Коды из `src/utils/keyboard.ts`: Enter 13, стрелки 37–40, Back 461, Red/Green/Yellow/Blue 403–406,
Play 415, Pause 19, Settings 10133. В плеере настройки открываются кнопкой Blue или ArrowUp.

## Диагностическая панель

Открыть: во время воспроизведения Blue (или Up), в меню пункт «Диагностика воспроизведения», OK.
Закрыть: Back. Снять содержимое:

```sh
../clean-webos-tv/tools/devtools.py run ../clean-webos-tv/profiles/kinopub.webos/diagnostics.js
```

Скрипт возвращает JSON: текст панели (если открыта), состояние `<video>` (время, буфер впереди,
`readyState`, `networkState`, ошибка, размер кадра, `getVideoPlaybackQuality`) и активный элемент
фокуса. Ручной чек-лист панели в `docs/playback-diagnostics-manual-test.md` репозитория приложения.

## Логи hls.js

hls.js создаётся в `src/components/media/media.new.tsx` (`new HLS({...})`) без `debug`, поэтому
в консоли только предупреждения и ошибки приложения. Что доступно без изменения кода:
- `devtools.py network --grep m3u8` и `--grep .ts`: плейлисты, фрагменты, статусы и отказы.
- Диагностическая панель: история событий `FRAG_*`, `LEVEL_SWITCHED`, `ERROR`, буфер.
- `devtools.py console`: ошибки и исключения приложения.

Подробный лог hls.js: локально, не коммитя, добавить `debug: true` в конфиг `new HLS({...})`,
собрать и поставить. Убрать перед коммитом.

## Типовой сценарий проверки

1. `devloop.sh`, дождаться главного экрана: `devtools.py screenshot shots/01-home.png`.
2. Открыть тайтл стрелками и Enter, запустить воспроизведение.
3. `devtools.py run .../diagnostics.js` раз в 5–10 секунд: время растёт, буфер впереди > 0.
4. Для сценария из issue (перемотка, смена качества, восстановление после ошибки) снимать
   `network` и `console` во время действия и панель после.
5. В отчёте разделять проверенное на G5 и проверенное в браузерных сценариях репозитория.

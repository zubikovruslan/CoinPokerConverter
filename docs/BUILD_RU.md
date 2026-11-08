# Сборка 0.1.0-beta.1

Рабочий выпуск — переносимая папка с нативным Windows x64 launcher и локальной
CPython 3.12.10. На компьютере пользователя Python не устанавливается.
Все файлы `runtime` и `app` должны оставаться рядом с exe.

Исходные компоненты получены с python.org и PyPI. Контрольные суммы включены
в `Package_manifest.json`. Нативный запуск Windows — отдельная ещё не выполненная
проверка; компиляция PE и проверка исходников её не заменяют.

Для воспроизведения сборки нужны Python 3.12 на машине разработчика, MinGW-w64
(`x86_64-w64-mingw32-gcc`, `x86_64-w64-mingw32-windres`), официальный
`python-3.12.10-embed-amd64.zip`, извлечённый `tcltk.msi` той же версии и колёса
из `requirements-build.txt`. В Linux MSI можно извлечь `msiextract -C target tcltk.msi`.
Пакеты компилятора и исходные загрузки не входят в пользовательский архив.

```sh
python3 -m unittest discover -s tests -v
python3 build_portable.py --python-embed /path/python-3.12.10-embed-amd64.zip --python-tcltk /path/extracted-tcltk --tcl-license /path/TCL_LICENSE.txt --wheels /path/wheels
```

Параметры `--cc` и `--windres` позволяют указать абсолютные пути к инструментам.
`TCL_LICENSE.txt` — неизменённый файл `license.terms` тега `core-8-6-15`
официального репозитория `https://github.com/tcltk/tcl`.
Существующая папка `dist/CoinPokerConverter` не перезаписывается. Для новой сборки
сначала перенесите предыдущую в архив. Скрипт сборки не скачивает компоненты сам.

CLI для воспроизводимой проверки из исходников:

```sh
python3 main.py --convert source.txt --hero TestPlayer --output result --history test_history.sqlite --result-json result.json
python3 main.py --convert source.txt --hero TestPlayer --mode new --output result --history test_history.sqlite --result-json new_result.json
```

GUI-проверка без настоящих данных: `tests/gui_flow.py` под работающим дисплеем.
Все публичные тестовые раздачи в `tests/fixtures.py` синтетические. Реальные истории,
личная история выгрузок и частные отчёты в пользовательскую сборку не включаются.

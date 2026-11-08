#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <wchar.h>
#include <stdlib.h>

/* Application-local interpreter. No PATH lookup, shell, registry or installer. */
int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance; (void)previous; (void)show;
    wchar_t base[32768], python[32768], script[32768], command[32768];
    DWORD length = GetModuleFileNameW(NULL, base, 32768);
    if (!length || length >= 32768) return 1;
    wchar_t *slash = wcsrchr(base, L'\\');
    if (!slash) return 1;
    *slash = 0;
    if (_snwprintf(python, 32768, L"%ls\\runtime\\pythonw.exe", base) < 0 ||
        _snwprintf(script, 32768, L"%ls\\main.py", base) < 0 ||
        _snwprintf(command, 32768, L"\"%ls\" -I \"%ls\" %ls", python, script, arguments) < 0) {
        MessageBoxW(NULL, L"Слишком длинный путь или команда запуска.", L"CoinPoker Converter", MB_OK | MB_ICONERROR);
        return 1;
    }
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES || GetFileAttributesW(script) == INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL, L"Распакуйте весь ZIP-архив. Папки runtime и app должны находиться рядом с CoinPokerConverter.exe.", L"CoinPoker Converter", MB_OK | MB_ICONERROR);
        return 1;
    }
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    startup.cb = sizeof(startup);
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, base, &startup, &process)) {
        wchar_t message[256];
        _snwprintf(message, 256, L"Не удалось запустить программу (ошибка Windows %lu). Повторно распакуйте полный архив.", GetLastError());
        MessageBoxW(NULL, message, L"CoinPoker Converter", MB_OK | MB_ICONERROR);
        return 1;
    }
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD status = 1;
    GetExitCodeProcess(process.hProcess, &status);
    CloseHandle(process.hProcess);
    return (int)status;
}

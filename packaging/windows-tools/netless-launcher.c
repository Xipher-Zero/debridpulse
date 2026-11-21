/* netless-launcher.exe -- DownloadPulse's Windows stand-in for a script's "#!".
 *
 * Windows starts only executables, and yt-dlp starts every Media Downloads
 * helper (ffmpeg, ffprobe, deno) from one executable path. On POSIX that path
 * is DownloadPulse's wrapper script, whose "#!" line makes the kernel run the
 * network-confinement shim (backend/executors/media/netless.py) before the
 * real tool. On Windows the media sandbox puts a copy of this launcher beside
 * each wrapper script, as <script>.exe; it does exactly what the kernel does
 * for "#!" and nothing else:
 *
 *   <dir>\ffmpeg.exe ARGS...  runs  INTERPRETER [OPTION] "<dir>\ffmpeg" ARGS...
 *
 * where "#!INTERPRETER [OPTION]" is the first line of <dir>\ffmpeg (the
 * interpreter path may be double-quoted). ARGS are passed on exactly as they
 * were received (the rest of this process's own command line). The
 * interpreter inherits this process's standard handles, runs in a job that
 * ends with this process, and its exit code is this process's. The launcher
 * confines nothing itself: the shim it runs does, and refuses to run the tool
 * otherwise.
 *
 * Built by packaging/windows-tools/build-windows.sh (mingw-w64, static,
 * kernel32 only).
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

#define FAIL 126

static int fail(const char *message)
{
    DWORD written;
    HANDLE err = GetStdHandle(STD_ERROR_HANDLE);
    if (err && err != INVALID_HANDLE_VALUE) {
        WriteFile(err, "netless-launcher: ", 18, &written, NULL);
        WriteFile(err, message, (DWORD)lstrlenA(message), &written, NULL);
        WriteFile(err, "\n", 1, &written, NULL);
    }
    return FAIL;
}

/* The rest of the command line after its first argument, as received. */
static const wchar_t *arguments_after_program(const wchar_t *line)
{
    if (*line == L'"') {
        for (line++; *line && *line != L'"'; line++) {}
        if (*line == L'"') line++;
    } else {
        while (*line && *line != L' ' && *line != L'\t') line++;
    }
    while (*line == L' ' || *line == L'\t') line++;
    return line;
}

int wmain(void)
{
    static wchar_t self[32768], interpreter[32768], option[64], command[32768];
    static char head[4096];
    wchar_t line[4096];
    DWORD length = GetModuleFileNameW(NULL, self, 32768), got = 0;
    if (length == 0 || length >= 32768 - 1 || length <= 4 || _wcsicmp(self + length - 4, L".exe") != 0)
        return fail("cannot name its own script");
    self[length - 4] = L'\0';                                   /* <dir>\ffmpeg.exe -> <dir>\ffmpeg */

    HANDLE script = CreateFileW(self, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (script == INVALID_HANDLE_VALUE) return fail("no wrapper script beside the launcher");
    BOOL read = ReadFile(script, head, sizeof head - 1, &got, NULL);
    CloseHandle(script);
    if (!read || got < 3 || head[0] != '#' || head[1] != '!') return fail("the wrapper script has no #! line");
    head[got] = '\0';
    for (DWORD i = 0; i < got; i++) if (head[i] == '\r' || head[i] == '\n') { head[i] = '\0'; break; }
    int converted = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, head + 2, -1, line, 4096);
    if (converted <= 0) return fail("the #! line is not UTF-8");

    const wchar_t *cursor = line;
    size_t n = 0;
    if (*cursor == L'"') {
        for (cursor++; *cursor && *cursor != L'"'; cursor++) interpreter[n++] = *cursor;
        if (*cursor != L'"') return fail("unterminated interpreter path");
        cursor++;
    } else {
        while (*cursor && *cursor != L' ' && *cursor != L'\t') interpreter[n++] = *cursor++;
    }
    interpreter[n] = L'\0';
    while (*cursor == L' ' || *cursor == L'\t') cursor++;
    if (n == 0 || wcslen(cursor) >= 64) return fail("unusable #! line");
    wcscpy(option, cursor);

    int written = _snwprintf(command, 32768, L"\"%ls\"%ls%ls \"%ls\" %ls", interpreter, *option ? L" " : L"",
                             option, self, arguments_after_program(GetCommandLineW()));
    if (written < 0 || written >= 32768) return fail("command line too long");

    /* The interpreter (and so the confined tool) ends with this launcher. */
    HANDLE job = CreateJobObjectW(NULL, NULL);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    ZeroMemory(&limits, sizeof limits);
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!job || !SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof limits))
        return fail("cannot create its job");

    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    ZeroMemory(&startup, sizeof startup);
    startup.cb = sizeof startup;
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    if (!CreateProcessW(interpreter, command, NULL, NULL, TRUE, CREATE_SUSPENDED, NULL, NULL, &startup, &process))
        return fail("cannot start the interpreter");
    if (!AssignProcessToJobObject(job, process.hProcess)) {
        TerminateProcess(process.hProcess, FAIL);
        return fail("cannot place the interpreter in its job");
    }
    ResumeThread(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD code = FAIL;
    GetExitCodeProcess(process.hProcess, &code);
    return (int)code;
}

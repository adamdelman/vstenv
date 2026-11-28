/* rtcheck.exe: threads for tests/test_wine_rt.py to look at with sched_getscheduler.
 * Each thread names itself (Wine passes the name on to the unix thread) and ends on its own.
 *   rtcheck basic  "tc": time-critical 3 s, then normal 3 s; "normal": normal 6 s
 *   rtcheck ext    "ext": 2 s, time-critical 2 s, normal 2 s (the test makes it FIFO 83 from outside)
 *   rtcheck spin N "spin1".."spinN": time-critical, spin 5 s; "idle": time-critical, 1 ms busy in 3 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

static double now(void) { LARGE_INTEGER f, c; QueryPerformanceFrequency(&f); QueryPerformanceCounter(&c); return (double)c.QuadPart / f.QuadPart; }
static void name(const char *n) { WCHAR w[16]; MultiByteToWideChar(CP_ACP, 0, n, -1, w, 16); SetThreadDescription(GetCurrentThread(), w); }

static DWORD WINAPI tc(void *p) { name("tc"); SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL); Sleep(3000);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_NORMAL); Sleep(3000); return 0; }
static DWORD WINAPI normal(void *p) { name("normal"); Sleep(6000); return 0; }
static DWORD WINAPI ext(void *p) { name("ext"); Sleep(2000); SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL); Sleep(2000);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_NORMAL); Sleep(2000); return 0; }
static DWORD WINAPI spin(void *p) { char n[16]; double end; sprintf(n, "spin%d", (int)(INT_PTR)p); name(n);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL); end = now() + 5; while (now() < end) ; return 0; }
static DWORD WINAPI idle(void *p) { double end, t; name("idle"); SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
    end = now() + 5; while ((t = now()) < end) { while (now() < t + 0.001) ; Sleep(2); } return 0; }

int main(int argc, char **argv)
{
    HANDLE h[66]; int n = 0, i, spins;
    if (argc < 2) return 2;
    if (!strcmp(argv[1], "basic")) { h[n++] = CreateThread(0, 0, tc, 0, 0, 0); h[n++] = CreateThread(0, 0, normal, 0, 0, 0); }
    else if (!strcmp(argv[1], "ext")) h[n++] = CreateThread(0, 0, ext, 0, 0, 0);
    else if (!strcmp(argv[1], "spin")) {
        spins = argc > 2 ? atoi(argv[2]) : 2; if (spins < 1 || spins > 64) return 2;
        for (i = 1; i <= spins; i++) h[n++] = CreateThread(0, 0, spin, (void *)(INT_PTR)i, 0, 0);
        h[n++] = CreateThread(0, 0, idle, 0, 0, 0);
    }
    else return 2;
    printf("started\n"); fflush(stdout);
    WaitForMultipleObjects(n, h, TRUE, 20000);
    printf("done\n"); return 0;
}

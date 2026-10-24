/*
 * PID 1 of an OpenSandbox container, and the subreaper every user command runs under.
 *
 *   sandbox-init <uid>                as PID 1: reap every child, and SIGKILL the ones
 *                                     running as <uid>
 *   sandbox-init --subreaper cmd ...  become a child subreaper, then exec cmd
 *
 * User commands run as <uid> under --subreaper, so whatever they start stays in their
 * subtree for as long as they run, however it detaches (setsid, double fork). A leftover
 * only reaches PID 1 once its command has ended, and is killed there: no process of
 * <uid> outlives the command that started it. Sandbox services run as root and are
 * only reaped.
 */
#define _GNU_SOURCE
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* The real uid: a process cannot change it, unlike the owner of /proc/<pid>. */
static long real_uid(long pid)
{
    char path[64], line[256];
    long uid = -1;
    snprintf(path, sizeof path, "/proc/%ld/status", pid);
    FILE *status = fopen(path, "r");
    if (!status)
        return -1;
    while (fgets(line, sizeof line, status))
        if (sscanf(line, "Uid: %ld", &uid) == 1)
            break;
    fclose(status);
    return uid;
}

static void kill_orphans(long uid)
{
    /* Nothing is reaped during the scan, so no PID read here can be reused before its kill. */
    FILE *children = fopen("/proc/1/task/1/children", "r");
    long pid;
    if (!children)
        return;
    while (fscanf(children, "%ld", &pid) == 1)
        if (real_uid(pid) == uid)
            kill((pid_t)pid, SIGKILL);
    fclose(children);
}

int main(int argc, char **argv)
{
    if (argc > 2 && strcmp(argv[1], "--subreaper") == 0) {
        if (prctl(PR_SET_CHILD_SUBREAPER, 1) == 0)
            execvp(argv[2], argv + 2);
        perror("sandbox-init");
        return 127;
    }
    if (argc != 2) {
        fprintf(stderr, "usage: sandbox-init <uid> | sandbox-init --subreaper cmd ...\n");
        return 2;
    }
    long uid = strtol(argv[1], NULL, 10);
    sigset_t chld;
    sigemptyset(&chld);
    sigaddset(&chld, SIGCHLD);
    sigprocmask(SIG_BLOCK, &chld, NULL);
    /* An orphan is adopted without any signal, so look for leftovers every second too. */
    const struct timespec tick = {1, 0};
    for (;;) {
        sigtimedwait(&chld, NULL, &tick);
        while (waitpid(-1, NULL, WNOHANG) > 0)
            ;
        kill_orphans(uid);
    }
}

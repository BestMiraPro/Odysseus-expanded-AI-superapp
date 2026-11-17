/*
 * odysseus-seat: run one command as one of the Council seat users.
 *
 *     odysseus-seat SEAT GROUP COMMAND [ARG...]
 *
 * Installed setuid root, mode 4750, group odysseus-launch: only the users in
 * that group (the entrypoint adds Odysseus's own user, nobody else) can run it.
 * SEAT and GROUP are numbers from 0 to SEAT_SLOTS - 1. The command runs as the
 * seat user SEAT_BASE + SEAT, with the group SEAT_BASE + GROUP as its primary
 * group and its own group as the only other one. The range is compiled in, so
 * it can start a process as a seat user and as no one else, root included.
 *
 * It keeps the caller's environment, working directory and standard streams:
 * Odysseus builds the environment from an allowlist (src/seat_user.py). It
 * replaces itself with COMMAND (same pid), after:
 *   - dropping every other supplementary group and setting real, effective
 *     and saved ids to the seat user's, then checking that root cannot be
 *     regained;
 *   - setting no_new_privs, so no setuid program can raise the seat again;
 *   - closing every file descriptor above 2;
 *   - setting umask 002, so the files the seat creates stay writable for the
 *     group of the folder they are in, which Odysseus's user is in.
 *
 * Built static (see the Dockerfile), so no dynamic loader or shared library
 * runs with root's privileges.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <grp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <unistd.h>

#if !defined(SEAT_BASE) || !defined(SEAT_SLOTS)
#error "build with -DSEAT_BASE=<first uid and gid> -DSEAT_SLOTS=<how many>"
#endif

static int fail(const char *what) {
    fprintf(stderr, "odysseus-seat: %s: %s\n", what, strerror(errno));
    return 126;
}

/* A whole number from 0 to SEAT_SLOTS - 1, or -1. */
static long slot(const char *text) {
    char *end;
    long value;

    errno = 0;
    value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value < 0 || value >= SEAT_SLOTS)
        return -1;
    return value;
}

int main(int argc, char **argv) {
    long seat, group;
    uid_t uid;
    gid_t gid, groups[2];

    if (argc < 4 || (seat = slot(argv[1])) < 0 || (group = slot(argv[2])) < 0) {
        fprintf(stderr, "usage: odysseus-seat SEAT GROUP COMMAND [ARG...]  (SEAT, GROUP: 0 to %d)\n",
                SEAT_SLOTS - 1);
        return 64;
    }
    if (getuid() >= SEAT_BASE && getuid() < SEAT_BASE + SEAT_SLOTS) {
        fprintf(stderr, "odysseus-seat: a seat user may not run this\n");
        return 126;
    }
    uid = SEAT_BASE + seat;
    gid = SEAT_BASE + group;
    groups[0] = gid;
    groups[1] = SEAT_BASE + seat;
    if (setgroups(2, groups) != 0)
        return fail("cannot drop supplementary groups");
    if (setresgid(gid, gid, gid) != 0)
        return fail("cannot switch to the seat group");
    if (setresuid(uid, uid, uid) != 0)
        return fail("cannot switch to the seat user");
    if (setuid(0) == 0 || setgid(0) == 0) {
        fprintf(stderr, "odysseus-seat: root could be regained, refusing to run\n");
        return 126;
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0)
        return fail("cannot set no_new_privs");
    if (close_range(3, ~0U, 0) != 0)
        return fail("cannot close inherited file descriptors");
    umask(002);
    execvp(argv[3], argv + 3);
    fprintf(stderr, "odysseus-seat: cannot run %s: %s\n", argv[3], strerror(errno));
    return errno == ENOENT ? 127 : 126;
}

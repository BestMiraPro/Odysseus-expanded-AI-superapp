/*
 * odysseus-seat: run one command as the Council seat user.
 *
 *     odysseus-seat COMMAND [ARG...]
 *
 * Installed setuid root, mode 4750, group odysseus-launch: only the users in
 * that group (the entrypoint adds Odysseus's own user, nobody else) can run it.
 * It takes no user argument. The seat user's ids are compiled in, so it can
 * start a process as the seat user and as no one else, root included.
 *
 * It keeps the caller's environment, working directory and standard streams:
 * Odysseus builds the environment from an allowlist (src/seat_user.py). It
 * replaces itself with COMMAND (same pid), after:
 *   - dropping every supplementary group and setting real, effective and saved
 *     ids to the seat user's, then checking that root cannot be regained;
 *   - setting no_new_privs, so no setuid program can raise the seat again;
 *   - closing every file descriptor above 2;
 *   - setting umask 002, so the files the seat creates in a run folder stay
 *     writable for the odysseus-seat group that Odysseus's user is in.
 *
 * Built static (see the Dockerfile), so no dynamic loader or shared library
 * runs with root's privileges.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <grp.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef SEAT_UID
#error "build with -DSEAT_UID=<uid> -DSEAT_GID=<gid>"
#endif

static int fail(const char *what) {
    fprintf(stderr, "odysseus-seat: %s: %s\n", what, strerror(errno));
    return 126;
}

int main(int argc, char **argv) {
    const uid_t uid = SEAT_UID;
    const gid_t gid = SEAT_GID;

    if (argc < 2) {
        fprintf(stderr, "usage: odysseus-seat COMMAND [ARG...]\n");
        return 64;
    }
    if (getuid() == uid) {
        fprintf(stderr, "odysseus-seat: the seat user may not run this\n");
        return 126;
    }
    if (setgroups(1, &gid) != 0)
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
    execvp(argv[1], argv + 1);
    fprintf(stderr, "odysseus-seat: cannot run %s: %s\n", argv[1], strerror(errno));
    return errno == ENOENT ? 127 : 126;
}

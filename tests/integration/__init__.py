"""Integration tests: real sshd, real rsync, real files.

Excluded by default (`-m "not integration"`). They need Docker and are the only
tests that can prove the tool's central promise: that a file owned by uid 999,
with an xattr, a hardlink and a sparse hole, actually arrives intact.
"""

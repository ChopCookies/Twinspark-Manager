"""The workstation client: reach a pair of Sparks from a laptop (macOS, Windows, Linux).

The user enters ONE address for node A (LAN IP / hostname, or a Tailscale address) and an SSH user.
Everything else follows from that:

* The client never opens a LAN listener. Every local port it offers (GUI, Hermes, coding agents) is
  bound to ``127.0.0.1`` by the ``ssh -L`` forwards it supervises (:class:`ssh.Tunnel`).
* Management traffic goes through an SSH forward to node A's management API, which listens on node A's
  loopback only (``127.0.0.1:8443`` by default). Inference goes through a second forward to node A's
  gateway (port ``8000``). Apps on the laptop use ``http://127.0.0.1:<local port>``.
* Tailscale mode is the same SSH transport over the node's tailnet address (``100.x.y.z`` or
  ``*.ts.net``); the mode is stored so the UI can say which path is in use.
* Direct mode is an explicit opt-in: an ``https://`` URL the user already exposed (for example with
  ``tsm remote tailscale-serve``); no SSH is involved.

Host keys are trust-on-first-use with an explicit confirmation and live in the client's own
``known_hosts``; authentication uses the user's agent/keys or a dedicated client key that is installed
once with the SSH password (the password is never stored). API keys fetched from node A live in the
OS keyring when one is usable, else in a 0600 file (:mod:`.secrets`).

Modules: :mod:`.paths` (where the client keeps its files), :mod:`.secrets` (credential store),
:mod:`.connections` (saved connections), :mod:`.ssh` (OpenSSH wrapper, host keys, key bootstrap,
tunnels), :mod:`.discovery` (what is installed on node A, its config and keys).

A first connect, in order: ``ssh.scan_host_keys`` -> show the fingerprints, the user confirms ->
``ssh.trust_host_keys`` -> ``discovery.probe_node`` (on an ``auth`` SSHError: ``ssh.ensure_client_key`` +
``ssh.install_key_with_password`` with a password the user types, then probe again) ->
``discovery.read_controller_nodes`` (remote ports and forward hosts) -> ``discovery.fetch_credentials``
(or keys pasted by the user) -> ``secrets.CredentialStore.set`` -> ``ssh.Tunnel.for_connection`` and
save the picked local ports with ``connections.ConnectionStore.update``.
"""

#!/bin/sh
# GIT_SSH_COMMAND wrapper: usa una chiave montata come bind-mount.
#
# Perché esiste: la chiave arriva nel container da un bind-mount di Docker
# Desktop, che le assegna 0555 (leggibile da tutti). Il client ssh rifiuta una
# chiave privata con quei permessi ("UNPROTECTED PRIVATE KEY FILE") e `chmod`
# sul mount non ha effetto — i permessi vengono dal bridge FS dell'host, non dal
# filesystem del container. L'unica via è copiarla in un path privato del
# container e usare quella.
#
# La copia sta in /tmp (tmpfs del container, non persistita) ed è 0600 di app.
set -eu

SRC="${GIT_SSH_KEY_PATH:-/run/secrets/git_ssh_key}"
DST=/tmp/.git_ssh_key

# -f oltre a -s: senza chiave montata il compose mette li' un placeholder, e una
# directory passerebbe il test di dimensione facendo fallire l'install (set -e),
# cioe' rompendo git proprio sui device che una chiave non ce l'hanno.
if [ -f "$SRC" ] && [ -s "$SRC" ]; then
    # `install -m` scrive permessi e contenuto in un colpo solo: nessuna
    # finestra in cui il file esiste con i permessi sbagliati.
    install -m 600 "$SRC" "$DST"
    exec ssh -i "$DST" \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile=/tmp/known_hosts \
        "$@"
fi

# Nessuna chiave montata (il caso normale: remote = origin su un host Linux).
exec ssh "$@"

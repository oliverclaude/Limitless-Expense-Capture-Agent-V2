# Install Hermes ntfy on limitlessfin

This starts the Hermes gateway at boot and connects it to `Fin-Limitless-V2`.
It does not replace the expense agent. Version 1 keeps running on `Fin-Limitless`.

The gateway process runs as the account that owns `~/.hermes`, not root. The unit sets `User` and `Group` to that account. Below, that account is `${HERMES_USER}`.
`sudo` is only used by you, once, to register that unit. The bot does not run it, and the service does not run as root.

Run these on limitlessfin as `${HERMES_USER}`, in this order.

## 1. Subscribe the phone

In the ntfy app, on the server you set as `NTFY_SERVER_URL`, subscribe to `Fin-Limitless-V2`.

## 2. Add the ntfy settings

Put the server address in `templates/ntfy.env` on the machine, then append that file to `~/.hermes/.env`. Do not add email settings. Do not commit the server address.

## 3. Remove the user-level gateway

A user service is already running, and it stops when the login session ends. A container needs the system service instead. Running both at once makes start and stop ambiguous.

```bash
hermes gateway stop
hermes gateway uninstall
```

`uninstall` deletes only `~/.config/systemd/user/hermes-gateway.service`. It does not delete `~/.hermes`.

## 4. Install the system service

This writes `/etc/systemd/system/hermes-gateway.service`, enables it for boot, and enables linger for `${HERMES_USER}`. It asks for your sudo password. The bot will not run this.

```bash
sudo hermes gateway install --system --run-as-user "$HERMES_USER" --start-on-login --no-start-now
```

The unit this command writes has the same shape as `templates/hermes-gateway.service`, with the placeholders filled in. Compare them if you want. Do not copy the file in by hand.

## 5. Start it

```bash
sudo hermes gateway start --system
sudo hermes gateway status --system
```

`hermes gateway status` without `--system` will look at the user service, which is gone. Use `--system`.

Logs:

```bash
journalctl -u hermes-gateway -f
```

## What the process is

systemd runs the Hermes launcher from `${HERMES_HOME}`:

```text
hermes gateway run
```

as `${HERMES_USER}`, with `HERMES_HOME` set to that account's Hermes home (usually `~/.hermes`). That process loads `~/.hermes/.env` and `config.yaml`, subscribes to `Fin-Limitless-V2`, and ticks scheduled jobs every 60 seconds. There is no second expense process.

Send a message to `Fin-Limitless-V2` from the phone. The reply should come back on that topic. Version 1 should stay silent, because it is still on `Fin-Limitless`.

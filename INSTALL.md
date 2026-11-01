# Install Hermes ntfy on limitlessfin

This starts the Hermes gateway at boot and connects it to `Fin-Limitless-V2-chat`. Claim replies stay on `Fin-Limitless-V2`.
It does not replace the expense agent. Version 1 keeps running on `Fin-Limitless`.

The gateway process runs as the account that owns `~/.hermes`, not root. The unit sets `User` and `Group` to that account. Below, that account is `${HERMES_USER}`.
`sudo` is only used by you, once, to register that unit. The bot does not run it, and the service does not run as root.

Run these on limitlessfin as `${HERMES_USER}`, in this order.

## 1. Subscribe the phone

In the ntfy app, on the server you set as `NTFY_SERVER_URL`, subscribe to `Fin-Limitless-V2` for claims and `Fin-Limitless-V2-chat` for the assistant.

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

as `${HERMES_USER}`, with `HERMES_HOME` set to that account's Hermes home (usually `~/.hermes`). That process loads `~/.hermes/.env` and `config.yaml`, subscribes to `Fin-Limitless-V2-chat`, and ticks scheduled jobs every 60 seconds. The claim listener is a separate user service. It is not a second copy of version 1.

Send a message to `Fin-Limitless-V2-chat` from the phone. The assistant should reply on that topic. A claim reply belongs on `Fin-Limitless-V2`. Version 1 should stay silent, because it is still on `Fin-Limitless`.

## 6. Install the expense program

Do this once, as `${HERMES_USER}`, after the gateway is running. Do not stop version 1. The bot does not run `sudo`.

```bash
cd "$AGENT_HOME"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x bin/expense-v2 scripts/expense-v2-poll.py
ln -sfn "$AGENT_HOME/bin/expense-v2" "$HOME/.local/bin/expense-v2"
```

`$AGENT_HOME` is this checkout. Put that path in `/etc/expense-agent-v2/env` as `AGENT_HOME`. On that same file, set `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, and `OLLAMA_API_KEY` to the local model Hermes already uses. Leave the server address out of git.

Point `skills.external_dirs` in `~/.hermes/config.yaml` at `$AGENT_HOME/skills`. Then record the mail job once:

```bash
expense-v2 install-cron
```

That creates one Hermes cron job named `expense-v2`, every `POLL_INTERVAL_MINUTES` (30), with no model in the job. The job's stdout stays empty. Pictures and words are sent by the program on `Fin-Limitless-V2`. The job does not read chat. Claim replies are the listener in the next section. Settings are answered on `Fin-Limitless-V2-chat`.

## 7. Listen for claim replies

The assistant must not subscribe to `Fin-Limitless-V2`. This user service does. It does not poll the mailbox. Changing the gateway topic in `~/.hermes/.env` needs a gateway restart, which the bot does not do.

```bash
expense-v2 install-listen
```

That writes `~/.config/systemd/user/expense-v2-listen.service` and enables it. No sudo. After you change `NTFY_TOPIC` in `~/.hermes/.env` to `Fin-Limitless-V2-chat`, restart the gateway yourself so it leaves the claim topic.

# Limitless Expense Capture Agent, version 2

Email in, one claim at a time. A Hermes job runs the program every 30 minutes. The program reads the slip, writes the month spreadsheet, and sends the picture and the words itself on `Fin-Limitless-V2`. A listener on that same topic reads the reply and asks the local model what it means, then logs, skips, updates, or asks again. The assistant is a different topic, `Fin-Limitless-V2-chat`. It does not see claim replies.

Version 1 is a separate program and a separate mailbox prefix (`claim:`). This repo does not stop it.

## Program

`agent.py` is the program. `poll` takes the newest `claimv2:` message and exits. `listen` stays running, reads replies on `Fin-Limitless-V2`, and passes each one to `capture`. `settings` lists the env file or changes a key that is already there, and that answer is sent on `Fin-Limitless-V2-chat`. `reply` remains as a manual command. The assistant does not use it. `install-cron` and `install-listen` are run by hand, once. The bot does not run them, and nothing here runs `sudo`.

`mail.py` talks to the mailbox. It accepts a subject only when it starts with `claimv2:` (case is ignored), checks the sender and the body password, saves the photo or PDF, and files the message into processed, needs review, failed, or rejected.

`extract.py` sends the slip to the local vision model, then applies the expense rules in code: whose money it was, the journal, VAT, and the comment taken from the subject. It does not call xAI.

`crop.py` asks the vision model for the four corners of the paper. It makes a polygon from those points and pulls it into a rectangle, using the longer horizontal edge as the width and the longer vertical edge as the height. Pixels outside that rectangle are dropped. If the model returns no corners, the older OpenCV crop is used.

`pdf_proof.py` reads the text of a digital PDF. If the PDF is a scan, it renders a page and hands that picture to `crop.py`.

`sheet.py` is the ledger. Each month has one workbook, `YYYY-MM-Expenses.xlsx`, with the same columns as version 1. It also finds a duplicate row and can delete one.

`state.py` stores the claim that is waiting for a person. The file lives under `AGENT_STATE_DIR`, not in version 1's queue. The chat and the scheduled job do not share a transcript. This file is the handoff.

`done_mail.py` sends the Done or Skipped email to the work address in the env file. It does not reply to whatever address was on the claim.

`ntfy_out.py` sends the picture, then the words, on `Fin-Limitless-V2`. Both messages are tagged `hermes-agent` so the claim listener does not treat them as a person typing. The assistant topic name is `Fin-Limitless-V2-chat`.

`replies.py` does not match the user's words. The claim listener asks the local model for the action. For a normal claim that is `confirm`, `skip`, `update`, or `ask`. For a duplicate it is `keep`, `remove_old`, `drop_new`, or `ask`. `ok` and `yes` are not a duplicate decision. `remove_old` means use the new line: the old sheet row and its proof files are deleted, then the new line is logged.

`settings.py` reads and writes the env file. Chat can ask for every current setting, or set any key that is already there. Password, secret, token, and api key values are shown as `(set)` or `(empty)`, not the secret itself. The poll interval is changed on the Hermes job before the file, so a failed schedule edit cannot leave the file ahead of the job. Other keys are picked up on the next run. Nothing here restarts the gateway.

## How it is started

`bin/expense-v2` is the chat command. It reads `AGENT_HOME` from the env file and runs `agent.py` with the repo's virtualenv. On the machine it is linked onto the gateway user's `PATH`.

`scripts/expense-v2-poll.py` is the scheduled entry. Hermes will not run a symlink that points outside its scripts directory, so `install-cron` copies this file to `~/.hermes/scripts/` and the job runs that copy. The copy only starts `agent.py poll`.

`skills/expense-v2/SKILL.md` is the Hermes skill for `Fin-Limitless-V2-chat`. It tells the model to run `expense-v2 settings` for a settings request, and to answer other conversation normally. It does not decide a claim.

`templates/expense-v2-listen.service` is the user service that runs `expense-v2 listen`. It reads `Fin-Limitless-V2` only. `install-listen` enables it for the gateway user. It does not use sudo, and it does not touch version 1 or the gateway unit.

## Config and install

`templates/expense-agent.env` is the env file with the secrets left blank. On the machine it is `/etc/expense-agent-v2/env`, mode 600. The server address for notifications and for the vision model are filled in there, not in git.

`templates/ntfy.env` is the block appended to `~/.hermes/.env` so the gateway subscribes to `Fin-Limitless-V2-chat`. The server address line is empty in git. Do not add email settings. The Hermes email adapter stays off. Claim messages stay on `Fin-Limitless-V2` in the expense env file.

`templates/hermes-gateway.service` shows the shape of the system unit. Do not copy it into place. `INSTALL.md` has the install command, which also turns on linger.

`INSTALL.md` is the install order: gateway first, then this program, the skill directory, and the one mail job.

`requirements.txt` is the Python packages for the virtualenv: image cropping, spreadsheets, and PDFs.

`.gitignore` keeps the virtualenv and compiled Python files out of git.

## Tests

`tests/test_rules.py` checks the subject prefix, the reply phrases, env edits, the spreadsheet row, and VAT. It does not open a mailbox and it does not call the vision model.

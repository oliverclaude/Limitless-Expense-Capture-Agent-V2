# Limitless Expense Capture Agent, version 2

Email in, one claim at a time. A Hermes job runs the program every 30 minutes. The program reads the slip, writes the month spreadsheet, and sends the picture and the words itself. Chat only decides which program command to run.

Version 1 is a separate program and a separate mailbox prefix (`claim:`). This repo does not stop it.

## Program

`agent.py` is the program. One run reads the env file and exits. `poll` takes the newest `claimv2:` message. `reply` applies a chat answer. `settings` lists the env file or changes a key that is already there. `install-cron` records the Hermes job. Run that last one by hand, once. The bot does not run it, and nothing here runs `sudo`.

`mail.py` talks to the mailbox. It accepts a subject only when it starts with `claimv2:` (case is ignored), checks the sender and the body password, saves the photo or PDF, and files the message into processed, needs review, failed, or rejected.

`extract.py` sends the slip to the local vision model, then applies the expense rules in code: whose money it was, the journal, VAT, and the comment taken from the subject. It does not call xAI.

`crop.py` asks the vision model for the four corners of the paper. It makes a polygon from those points and pulls it into a rectangle, using the longer horizontal edge as the width and the longer vertical edge as the height. Pixels outside that rectangle are dropped. If the model returns no corners, the older OpenCV crop is used.

`pdf_proof.py` reads the text of a digital PDF. If the PDF is a scan, it renders a page and hands that picture to `crop.py`.

`sheet.py` is the ledger. Each month has one workbook, `YYYY-MM-Expenses.xlsx`, with the same columns as version 1. It also finds a duplicate row and can delete one.

`state.py` stores the claim that is waiting for a person. The file lives under `AGENT_STATE_DIR`, not in version 1's queue. The chat and the scheduled job do not share a transcript. This file is the handoff.

`done_mail.py` sends the Done or Skipped email to the work address in the env file. It does not reply to whatever address was on the claim.

`ntfy_out.py` sends the picture, then the words, on the version 2 topic. Both messages are tagged `hermes-agent` so the gateway does not treat them as a person typing.

`replies.py` does not match the user's words. The chat agent passes `--action`. For a normal claim that is `confirm`, `skip`, `update`, or `ask`. For a duplicate it is `keep`, `remove_old`, `drop_new`, or `ask`. `ok` and `yes` are not a duplicate decision. `remove_old` means use the new line: the old sheet row and its proof files are deleted, then the new line is logged. The same file still recognises a direct request to change the high-value amount, add a journal, or set the minutes between mail checks.

`settings.py` reads and writes the env file. Chat can ask for every current setting, or set any key that is already there. Password, secret, token, and api key values are shown as `(set)` or `(empty)`, not the secret itself. The poll interval is changed on the Hermes job before the file, so a failed schedule edit cannot leave the file ahead of the job. Other keys are picked up on the next run. Nothing here restarts the gateway.

## How it is started

`bin/expense-v2` is the chat command. It reads `AGENT_HOME` from the env file and runs `agent.py` with the repo's virtualenv. On the machine it is linked onto the gateway user's `PATH`.

`scripts/expense-v2-poll.py` is the scheduled entry. Hermes will not run a symlink that points outside its scripts directory, so `install-cron` copies this file to `~/.hermes/scripts/` and the job runs that copy. The copy only starts `agent.py poll`.

`skills/expense-v2/SKILL.md` is the Hermes skill. For a claim answer it tells the model to run `expense-v2 reply`. To list or change settings it tells the model to run `expense-v2 settings`. The model does not send notifications, write the sheet, or run `poll`.

## Config and install

`templates/expense-agent.env` is the env file with the secrets left blank. On the machine it is `/etc/expense-agent-v2/env`, mode 600. The server address for notifications and for the vision model are filled in there, not in git.

`templates/ntfy.env` is the block appended to `~/.hermes/.env` so the gateway subscribes to `Fin-Limitless-V2`. The server address line is empty in git. Do not add email settings. The Hermes email adapter stays off.

`templates/hermes-gateway.service` shows the shape of the system unit. Do not copy it into place. `INSTALL.md` has the install command, which also turns on linger.

`INSTALL.md` is the install order: gateway first, then this program, the skill directory, and the one mail job.

`requirements.txt` is the Python packages for the virtualenv: image cropping, spreadsheets, and PDFs.

`.gitignore` keeps the virtualenv and compiled Python files out of git.

## Tests

`tests/test_rules.py` checks the subject prefix, the reply phrases, env edits, the spreadsheet row, and VAT. It does not open a mailbox and it does not call the vision model.

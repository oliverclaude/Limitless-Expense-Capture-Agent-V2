---
name: expense-v2
description: "Limitless expense claims on the Fin-Limitless-V2 chat. Use when the user replies about a claim, a duplicate, a slip, ok, skip, Keep both, Remove the old one, Don't capture the new one, the high-value amount, adding a journal, or how often mail is checked."
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [expense, claims, ntfy]
---

# Expense capture, version 2

The program does the work. You decide which command to run, then you stop talking.

## When to run it

Run the command when the message is about a waiting claim or a setting. That includes ok, skip, a field correction, and these sentences exactly:

- Keep both
- Remove the old one
- Don't capture the new one

Also run it to change the high-value amount, add a journal by name, or set how many minutes between mail checks.

For any other conversation, answer normally. Do not run the command.

## Command

Pass the user's words through unchanged, as one argument:

```bash
expense-v2 reply --text 'the user message'
```

## After the command

Your entire final answer is exactly `[SILENT]`.

The program already sent the ntfy message. Do not repeat it. Do not send ntfy yourself. Do not invent a curl command. Do not run sudo. Do not run `expense-v2 poll`. The schedule does that.

## What you do not decide

You do not choose whether a duplicate is kept. If the user did not use one of the three sentences, the program asks again. You still answer `[SILENT]`.

You do not write the spreadsheet, move mail, or delete proofs. The program does that.

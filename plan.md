## Goal
Make `~/projects/mitmproxy-mcp` A cli tool `odda` instead of an mcp server.

## Why
- agents are very good at bash
- it can be combined with other cli tools (via piping for example ) to give it more power
- easier to debug

## plan 
- the tool includes a skill and a opencode plugin (maybe include other harnesses in the future like pi)
- the skill describes the cli tool and it's commands and usage
- the plugin hooks to session start and launches a background process hooked to main (harness) process that exits when the harness exits. the background process is used to hold state and to launche browser and communicate with it. it also acts as a parent process for the browser so the browser exits when the harness closes
- the cli tools communicate with background process to communicate with the browser.


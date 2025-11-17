# meshcore_multitcp

multiplexing a mescore tcp-connected companion to multiple clients as cli-chat, observer or bots

## what you can do with this software

- use one companion with multiple client-apps simultaneously

## what you will need

- companion radio with wifi-firmware to allow tcp-connections
- linux device (tested under debian trixie)
- python3
- python meshcore package (pip install meshcore)

## usage

python3 meshcore_multitcp.py -d Device-IP:PORT -s Server-IP:PORT [-q|-v]

-d sets IP & port of your companion radio
-s sets IP & port of the machine this script listens to clients
-q minimizes CLI-output
-v maximizes CLI-output

After meshcore_multitcp is running you can connect your clients to IP/port set with -s

## what you should know before you start

This software comes as it is without any guarantee to work stable and secure.
It contains modified parts of the original meshcore_py-scripts.
There are several things untested und a lot of bugs in it. 
Some client-app functions doesn't work as expected and could throw timeout-errors or crash the whole app.

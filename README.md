diablo4 loh fishing bot v0.2

changelog: 

- added potion usage :

POTION_ENABLED       = True   # set False to disable entirely
POTION_KEY           = "q"
POTION_EVERY_N_KILLS = 1      # change to 2, 3, etc.


auto-casts, detects bite prompt via multi-template matching, reels in.
on timeout (mob from water), automatically attacks with right mosue click x4.


requirements:
    pip install mss opencv-python numpy pynput pyautogui

usage:
 change by your binds

CAST_KEY  = "`"
REEL_KEY  = "1"

    1. stand your character at the fishing spot
    2. fish manually a few times and capture the bite indicator at different
       animation frames save them as:
           bite_template_0.png
           bite_template_1.png
           bite_template_2.png
    3. run this script
    4. press F5 to start / pause the bot
    5. press F6 to quit

tested location screenshots: map1.png and map2.png

greetings to KUNSH

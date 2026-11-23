"""Devanagari -> casual Roman Hinglish (how Indians actually type: 'bhai kya kar raha hai').
Rule based with schwa deletion + a small dictionary of very common words."""
from __future__ import annotations

import re

CONS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
    "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d",
    "ध": "dh", "न": "n", "प": "p", "फ": "f", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r",
    "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "d", "ढ़": "dh", "फ़": "f", "य़": "y",
}
NUKTA_FIX = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f"}
VOW = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai",
       "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
MATRA = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o",
         "ौ": "au", "ॉ": "o", "ॅ": "e"}
VIRAMA, NUKTA = "्", "़"
ANUSVARA = {"ं": "n", "ँ": "n", "ः": "h"}
DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}

COMMON = {
    "है": "hai", "हैं": "hain", "मैं": "main", "में": "mein", "नहीं": "nahi", "क्या": "kya", "भाई": "bhai", "बाई": "bhai", "भाइ": "bhai",
    "यह": "yeh", "ये": "ye", "वह": "woh", "वो": "wo", "हाँ": "haan", "हां": "haan", "तो": "toh", "को": "ko",
    "का": "ka", "की": "ki", "के": "ke", "कि": "ki", "और": "aur", "भी": "bhi", "था": "tha", "थी": "thi",
    "हो": "ho", "गया": "gaya", "गई": "gayi", "रहा": "raha", "रही": "rahi", "कर": "kar", "करो": "karo",
    "अरे": "arey", "अबे": "abe", "चल": "chal", "चलो": "chalo", "मुझे": "mujhe", "तुम": "tum", "तू": "tu",
    "आप": "aap", "हम": "hum", "मेरा": "mera", "मेरी": "meri", "तेरा": "tera", "तेरी": "teri", "अब": "ab",
    "बस": "bas", "एक": "ek", "दो": "do", "कैसे": "kaise", "क्यों": "kyun", "कहाँ": "kahan", "कहां": "kahan",
    "कहा": "kaha", "यार": "yaar", "बहुत": "bahut", "अच्छा": "accha", "ठीक": "theek", "पता": "pata",
    "देख": "dekh", "देखो": "dekho", "मत": "mat", "सब": "sab", "कुछ": "kuch", "लिए": "liye", "साथ": "saath",
    "पे": "pe", "पर": "par", "से": "se", "ने": "ne", "जो": "jo", "जा": "ja", "आ": "aa", "मार": "maar",
    "गाड़ी": "gaadi", "गाडी": "gaadi", "पुलिस": "police", "गेम": "game", "ओके": "okay", "सॉरी": "sorry",
    "ब्रो": "bro", "कार": "car", "गन": "gun", "मिशन": "mission", "स्टेशन": "station", "गैस": "gas",
    "रिपेयर": "repair", "रिपेर": "repair", "शॉप": "shop", "वाह": "waah", "हाहा": "haha", "हाहाहा": "hahaha",
    "ओ": "oh", "ओह": "oh", "अरेरे": "arey arey", "इसको": "isko", "उसको": "usko", "इसे": "ise", "उसे": "use",
    "किया": "kiya", "दिया": "diya", "लिया": "liya", "होगा": "hoga", "होगी": "hogi", "सकता": "sakta",
}

_DEV = re.compile(r"[ऀ-ॿ]+")


def _word(w: str) -> str:
    if w in COMMON:
        return COMMON[w]
    out: list[str] = []          # list of (consonant/vowel chunks)
    inherent: list[int] = []      # indexes in out holding an inherent 'a'
    i, n = 0, len(w)
    while i < n:
        c = w[i]
        nxt = w[i + 1] if i + 1 < n else ""
        if c in CONS or (c in NUKTA_FIX and nxt == NUKTA):
            base = CONS.get(c, "")
            if nxt == NUKTA and c in NUKTA_FIX:
                base = NUKTA_FIX[c]
                i += 1
                nxt = w[i + 1] if i + 1 < n else ""
            out.append(base)
            if nxt == VIRAMA:
                i += 2
                continue
            if nxt in MATRA:
                out.append(MATRA[nxt])
                i += 2
                continue
            out.append("a")
            inherent.append(len(out) - 1)
            i += 1
            continue
        if c in VOW:
            out.append(VOW[c])
        elif c in MATRA:
            out.append(MATRA[c])
        elif c in ANUSVARA:
            out.append(ANUSVARA[c])
        elif c in DIGITS:
            out.append(DIGITS[c])
        i += 1
    # schwa deletion: final inherent a dropped (unless word is a single syllable)
    if inherent and inherent[-1] == len(out) - 1 and len(inherent) > 1 or (inherent and inherent[-1] == len(out) - 1 and len(out) > 2):
        out[inherent[-1]] = ""
    # medial: in V C a C V patterns drop the 'a' (kar-na -> karna, samajh-ta)
    for idx in reversed(inherent):
        if idx == len(out) - 1:
            continue
        if 2 <= idx and idx + 2 < len(out) and out[idx - 2] and out[idx - 2][-1] in "aeiou" \
                and out[idx + 1] and out[idx + 1][0] not in "aeiou" and out[idx + 2] and out[idx + 2][0] in "aeiou":
            out[idx] = ""
    s = "".join(out)
    s = s.replace("aa", "aa").replace("ee", "ee")
    return s


def to_roman(text: str) -> str:
    return _DEV.sub(lambda m: _word(m.group(0)), text).replace("।", ".").strip()


if __name__ == "__main__":
    import sys
    print(to_roman(" ".join(sys.argv[1:]) or "भाई ये तो काफी दूर है गैस स्टेशन में तो रिपेर होगी समझता करना"))

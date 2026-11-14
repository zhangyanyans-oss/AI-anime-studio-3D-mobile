# -*- coding: utf-8 -*-
"""AU 表情对照词库与关键词提取器。

AU = Facial Action Coding System (FACS) Action Unit，面部动作编码系统的标准单元。
用途：从生图/生视频提示词中智能捕捉表情与动作关键词，映射到标准 AU 编码，
可用于：表情一致性检查、跨镜头表情对齐、表情驱动工具（Live2D / Audio2Face /
ComfyUI 表情控制节点）的参数参考。

提取策略：
1. 本地词库子串匹配（零成本、毫秒级，覆盖中英文常用表达）
2. 词库未命中时由 main.py 调 DeepSeek 兜底智能映射（用下方 AU_TABLE 当对照表）
"""

# ---------- 单个 AU 速查表（人类可读对照表，也是 AI 兜底映射的依据） ----------
AU_TABLE = {
    "AU1":  "内眉上抬（担忧、悲伤的开端）",
    "AU2":  "外眉上抬（惊讶、挑眉）",
    "AU4":  "眉毛下压/皱眉（生气、专注、难过）",
    "AU5":  "上眼睑抬高（瞪眼、惊讶）",
    "AU6":  "脸颊上抬（真笑的标志，眼角挤出鱼尾纹）",
    "AU7":  "眼睑收紧（眯眼、怀疑、审视）",
    "AU9":  "鼻子皱起（厌恶）",
    "AU10": "上唇上抬（厌恶、狞笑）",
    "AU12": "嘴角上拉（微笑的核心单元）",
    "AU14": "嘴角收紧出现酒窝（冷笑、轻蔑、尴尬陪笑）",
    "AU15": "嘴角下拉（悲伤、撇嘴、失望）",
    "AU17": "下巴上抬（撅嘴、强忍情绪）",
    "AU18": "嘴唇前撅（嘟嘴、要亲亲）",
    "AU20": "嘴唇横向拉伸绷直（恐惧、紧张）",
    "AU22": "嘴唇撮成筒状（吹口哨、呸）",
    "AU23": "嘴唇收紧（咬牙、压抑愤怒）",
    "AU24": "嘴唇压紧（抿嘴、忍耐、尴尬）",
    "AU25": "双唇分开（张嘴、说话）",
    "AU26": "下巴放下、张颌（惊讶、大喊）",
    "AU27": "嘴大张拉伸（怒吼、尖叫、打哈欠）",
    "AU28": "嘴唇吮吸（咬唇、紧张）",
    "AU38/39": "鼻孔扩张/收缩（暴怒、极度恐惧的前兆）",
    "AU43": "闭眼（闭目、熟睡、放空）",
    "AU45": "眨眼（瞬目、使眼色）",
    "AU53": "头上仰（抬头、傲慢、看天）",
    "AU54": "头低垂（低头、沮丧、羞愧）",
    "AU55/56": "头左倾/右倾（歪头）",
    "AU57/58": "头左转/右转（回头、张望）",
}

# ---------- 表情/动作关键词 → AU 组合词库 ----------
# keywords 命中方式：对提示词做不区分大小写的子串匹配（中英文通用）
AU_SETS = [
    # --- 笑 ---
    {"au": "AU12", "label": "微笑",
     "keywords": ["微笑", "浅笑", "含笑", "嘴角上扬", "嘴角微微上扬", "笑意", "smile", "smiling", "slight smile"]},
    {"au": "AU6+AU12+AU25", "label": "开心大笑/真心笑（眼角带笑才算真笑）",
     "keywords": ["大笑", "开心", "高兴", "快乐", "灿烂的笑容", "开怀", "眼睛弯成月牙", "笑得很", "laugh", "laughing", "joy", "happy", "cheerful", "grin", "beaming"]},
    {"au": "AU12+AU6", "label": "温柔微笑",
     "keywords": ["温柔地笑", "温柔微笑", "温柔的笑", "温暖的笑", "gentle smile", "warm smile", "soft smile"]},
    {"au": "AU12(单侧)+AU14", "label": "冷笑/坏笑（文章：颧大肌单侧收缩）",
     "keywords": ["冷笑", "坏笑", "狞笑", "狡黠地笑", "冷冷一笑", "不屑冷笑", "smirk", "sneer"]},
    {"au": "AU12+AU53", "label": "得意（笑+抬头）",
     "keywords": ["得意", "扬扬得意", "沾沾自喜", "骄傲地笑", "smug", "triumphant"]},
    {"au": "AU14+AU24", "label": "尴尬陪笑",
     "keywords": ["尴尬", "窘迫", "干笑", "讪笑", "awkward", "embarrassed"]},
    {"au": "AU12(单侧)", "label": "窃笑/憋笑",
     "keywords": ["窃笑", "偷笑", "憋笑", "嘴角一勾", "snicker", "giggle", "snigger"]},
    {"au": "AU15+AU12(单侧)", "label": "苦笑/自嘲",
     "keywords": ["苦笑", "自嘲", "无奈地笑", "自嘲地", "bitter smile", "self-mocking", "wry smile"]},
    {"au": "AU12(单侧)+AU7", "label": "轻佻/挑逗",
     "keywords": ["轻佻", "挑逗", "调戏", "挑眉坏笑", "媚眼", "flirty", "teasing", "seductive", "playful smirk"]},
    {"au": "AU12+AU6+AU55/AU56", "label": "宠溺（柔笑+微歪头）",
     "keywords": ["宠溺", "慈爱", "疼爱", "温柔地注视", "doting", "affectionate", "loving gaze", "fondly"]},
    # --- 眉眼 ---
    {"au": "AU4", "label": "皱眉",
     "keywords": ["皱眉", "眉头紧锁", "眉心拧", "愁眉", "眉头紧蹙", "frown", "furrowed brow", "knit brows"]},
    {"au": "AU1+AU2", "label": "挑眉",
     "keywords": ["挑眉", "扬起眉毛", "眉梢上挑", "扬眉", "raise eyebrows", "raised brow"]},
    {"au": "AU6+AU7", "label": "眯眼",
     "keywords": ["眯起眼睛", "眯眼", "眯着眼睛", "眯缝着眼", "squint", "narrowed eyes"]},
    {"au": "AU5", "label": "瞪大眼睛",
     "keywords": ["瞪大眼睛", "睁大眼睛", "圆睁双眼", "眼睛瞪得", "wide-eyed", "eyes wide open"]},
    {"au": "AU7+AU4", "label": "审视/怀疑的眼神",
     "keywords": ["怀疑", "审视", "狐疑", "警惕地看", "戒备", "suspicious", "wary", "skeptical", "distrustful"]},
    {"au": "AU43", "label": "闭眼",
     "keywords": ["闭上眼睛", "闭眼", "闭目", "双眼紧闭", "eyes closed", "closed eyes"]},
    {"au": "AU45", "label": "眨眼/使眼色",
     "keywords": ["眨眼", "眨了眨", "wink", "blinking", "blinks"]},
    {"au": "AU43+AU54", "label": "熟睡/昏睡",
     "keywords": ["睡着", "熟睡", "昏睡", "沉睡", "asleep", "sleeping"]},
    {"au": "AU43(半闭)+AU45", "label": "困倦/睡眼惺忪",
     "keywords": ["困倦", "昏昏欲睡", "睡眼惺忪", "眼皮打架", "没精打采地眨眼", "drowsy", "sleepy", "droopy eyelids", "dozing"]},
    {"au": "AU27+AU43", "label": "打哈欠",
     "keywords": ["打哈欠", "哈欠", "呵欠", "yawn", "yawning"]},
    {"au": "AU43(半闭)+AU25", "label": "如释重负（眉头舒展+呼气）",
     "keywords": ["如释重负", "松了口气", "长舒一口气", "终于放心", "relief", "relieved"]},
    # --- 惊讶/恐惧 ---
    {"au": "AU1+AU2+AU5+AU26", "label": "惊讶（眉抬+瞪眼+张颌）",
     "keywords": ["惊讶", "吃惊", "惊愕", "意外", "难以置信", "惊呆", "surprise", "surprised", "astonished", "amazed"]},
    {"au": "AU1+AU2+AU5+AU25+AU27", "label": "震惊（文章级全开：惊到张大嘴）",
     "keywords": ["震惊", "目瞪口呆", "呆住", "下巴都掉了", "惊掉下巴", "stunned", "dumbfounded", "gobsmacked", "jaw dropped"]},
    {"au": "AU1+AU2+AU4", "label": "恐慌前兆/不安（恐惧眉）",
     "keywords": ["不安", "忐忑", "心里发毛", "隐隐的担忧", "预感不妙", "unease", "apprehension", "anxious", "dread"]},
    {"au": "AU38/AU39", "label": "鼻孔扩张（暴怒/极度恐惧前兆）",
     "keywords": ["鼻孔扩张", "鼻翼扇动", "鼻翼翕动", "nostrils flared", "nostril flare"]},
    {"au": "AU4+AU10+AU25+AU27", "label": "爆发大吼",
     "keywords": ["大吼", "怒吼", "咆哮", "嘶吼", "怒骂", "roar", "screaming", "howling"]},
    {"au": "AU4+AU5+AU20+AU25", "label": "恐惧/惊恐",
     "keywords": ["恐惧", "害怕", "惊恐", "吓得", "惊慌", "战栗", "fear", "terrified", "scared", "panic", "dread"]},
    # --- 悲伤 ---
    {"au": "AU1+AU4+AU15", "label": "悲伤",
     "keywords": ["悲伤", "难过", "伤心", "哀伤", "忧伤", "委屈", "低落", "sad", "sorrow", "grief", "heartbroken", "melancholy"]},
    {"au": "AU1+AU4+AU15+AU17+AU25", "label": "哭泣",
     "keywords": ["哭泣", "泪流满面", "含泪", "泪", "哽咽", "抽泣", "cry", "crying", "tears", "weeping", "sob"]},
    {"au": "AU1+AU2+AU7+AU15", "label": "含泪强忍（眼周收紧憋泪）",
     "keywords": ["强忍泪水", "含泪强忍", "眼眶湿润忍住", "忍住眼泪", "holding back tears", "teary-eyed"]},
    {"au": "AU15+AU54", "label": "沮丧/失望（撇嘴+低头）",
     "keywords": ["沮丧", "失望", "泄气", "垂头丧气", "意志消沉", "disappointed", "dejected", "depressed", "defeated"]},
    # --- 愤怒 ---
    {"au": "AU4+AU5+AU17+AU23", "label": "愤怒（皱眉+瞪眼+咬紧）",
     "keywords": ["愤怒", "生气", "恼火", "怒视", "暴怒", "青筋", "气得", "angry", "anger", "furious", "rage", "glare", "outraged"]},
    {"au": "AU23+AU24", "label": "压抑怒气（咬牙/抿嘴）",
     "keywords": ["咬牙", "咬紧牙关", "抿紧嘴唇", "抿着嘴", "强忍", "压抑着怒", "clenched jaw", "gritted teeth", "pressed lips"]},
    # --- 厌恶/轻蔑 ---
    {"au": "AU9+AU10+AU17", "label": "厌恶",
     "keywords": ["厌恶", "嫌弃", "恶心", "反感", "作呕", "鄙夷地皱鼻", "disgust", "disgusted", "revolted"]},
    {"au": "AU12+AU14+AU53", "label": "轻蔑/不屑",
     "keywords": ["轻蔑", "不屑", "鄙视", "瞧不起", "傲慢", "倨傲", "contempt", "scorn", "disdain", "haughty"]},
    # --- 嘴部动作 ---
    {"au": "AU25+AU26", "label": "张嘴/张大嘴",
     "keywords": ["张开嘴", "张嘴", "张大嘴巴", "张大了嘴", "open mouth", "opened mouth", "agape", "jaw drop"]},
    {"au": "AU25", "label": "说话/喊叫",
     "keywords": ["说着", "开口", "喊道", "大喊", "呼喊", "喊叫", "低声说", "speaking", "shouting", "yelling", "talking", "murmur"]},
    {"au": "AU18", "label": "嘟嘴/撅嘴",
     "keywords": ["嘟嘴", "嘟着嘴", "撅嘴", "撅着嘴", "鼓起腮", "pout", "pouting"]},
    {"au": "AU28", "label": "咬唇",
     "keywords": ["咬唇", "咬着嘴唇", "咬住下唇", "bite lip", "biting lip"]},
    # --- 头部动作 ---
    {"au": "AU53", "label": "抬头/仰头",
     "keywords": ["抬头", "仰头", "昂首", "仰起脸", "head up", "looking up", "tilt head back"]},
    {"au": "AU54", "label": "低头/垂头",
     "keywords": ["低头", "垂头", "耷拉着脑袋", "低下头", "head down", "looking down", "bowed head"]},
    {"au": "AU55/AU56", "label": "歪头（左右由画面决定）",
     "keywords": ["歪头", "偏着头", "歪着脑袋", "歪了歪头", "head tilt", "tilting head"]},
    {"au": "AU57/AU58", "label": "转头/回头张望",
     "keywords": ["回头", "转头", "扭头", "回望", "turn head", "looking back", "glance back"]},
    {"au": "头部动作（无标准AU）", "label": "点头/摇头",
     "keywords": ["点头", "摇头", "颔首", "nod", "shake head"]},
    # --- 特殊状态 ---
    {"au": "AU6+AU12", "label": "害羞（羞笑+泛红）",
     "keywords": ["害羞", "脸红", "羞涩", "羞怯", "shy", "blushing", "bashful", "flustered"]},
    {"au": "无AU激活", "label": "面无表情/冷漠（脸部肌肉放松）",
     "keywords": ["面无表情", "冷漠", "无表情", "呆滞", "放空", "淡漠", "expressionless", "blank", "numb", "stoic"]},

    # ===== v2 扩充（2026-11-13）：补齐常见情绪词 + 复合情绪 =====
    # 用户反馈"分析不到全部词语"（例：焦急、着急、焦灼 全漏）。原词库条目偏少，
    # 一个词没收录就整条漏掉。以下按情绪族补齐同义/成语写法；复合情绪单独给组合
    # AU，让一句话里的多个表情同时命中、AU 叠加。关键词只放"不可能是外貌服饰"的词。
    # --- 焦急 / 急躁 ---
    {"au": "AU4+AU20+AU7", "label": "焦急（皱眉+唇绷直+眼紧）",
     "keywords": ["焦急", "着急", "焦灼", "焦躁", "心焦", "急得", "急不可耐", "着急地",
                  "anxious", "anxiously", "agitated", "fretful"]},
    {"au": "AU4+AU20+AU26", "label": "心急如焚/坐立不安",
     "keywords": ["心急如焚", "心急火燎", "坐立不安", "坐卧不宁", "火烧眉毛", "寝食难安",
                  "restless", "fidgety", "on pins and needles"]},
    {"au": "AU2+AU5+AU25", "label": "迫不及待",
     "keywords": ["迫不及待", "急不可待", "恨不得", "催促", "impatient", "eagerly"]},
    {"au": "AU4+AU9+AU10", "label": "烦躁/不耐烦",
     "keywords": ["烦躁", "不耐烦", "心烦", "恼火地", "焦躁不安", "irritated", "impatient", "annoyed"]},
    {"au": "AU4+AU1+AU20", "label": "心慌/慌乱",
     "keywords": ["心慌", "慌乱", "慌张", "慌了", "六神无主", "手忙脚乱", "flustered", "panicked"]},
    # --- 专注 / 紧绷 / 隐忍 ---
    {"au": "AU4+AU7+AU23", "label": "专注/凝神（眉压+眼睑收紧+唇紧）",
     "keywords": ["专注", "凝神", "全神贯注", "聚精会神", "屏息", "凝望", "认真",
                  "focused", "concentrating", "attentive"]},
    {"au": "AU20+AU24+AU4", "label": "紧张/局促（唇绷直+抿嘴+眉压）",
     "keywords": ["紧张", "紧绷", "局促", "手足无措", "心跳加速", "攥紧", "捏紧",
                  "tense", "nervous", "on edge"]},
    {"au": "AU23+AU24+AU4", "label": "隐忍/克制情绪",
     "keywords": ["隐忍", "克制", "强忍着", "绷着", "压抑", "忍耐", "restrained", "holding back"]},
    {"au": "AU4+AU7", "label": "警惕/防备",
     "keywords": ["警惕", "防备", "警觉", "戒备", "提防", "alert", "on guard", "vigilant"]},
    {"au": "AU4+AU17+AU23", "label": "坚毅/决绝",
     "keywords": ["坚毅", "坚决", "决绝", "坚定", "毅然", "下定决心", "resolute", "determined"]},
    {"au": "AU4+AU5+AU23", "label": "狠厉/戾气",
     "keywords": ["狠厉", "发狠", "戾气", "狠戾", "杀气", "menacing", "ruthless"]},
    {"au": "AU43(半闭)+AU7", "label": "疲惫/倦怠",
     "keywords": ["疲惫", "疲倦", "倦怠", "累极了", "憔悴", "exhausted", "weary", "fatigued"]},
    # --- 犹豫 / 为难 / 欲言又止 ---
    {"au": "AU4+AU25+AU24", "label": "犹豫/迟疑",
     "keywords": ["犹豫", "迟疑", "踌躇", "举棋不定", "拿不定主意", "hesitant", "hesitating"]},
    {"au": "AU25+AU24+AU17", "label": "欲言又止/说不出口",
     "keywords": ["欲言又止", "欲语还休", "欲说还休", "话到嘴边", "张了张嘴",
                  "speechless", "at a loss for words"]},
    {"au": "AU1+AU4+AU15", "label": "为难/无奈",
     "keywords": ["为难", "无奈", "无可奈何", "没有办法", "helpless", "resigned"]},
    # --- 出神 / 垂眸 ---
    {"au": "AU43(半闭)+无AU激活", "label": "出神/发呆/恍惚",
     "keywords": ["出神", "发呆", "失神", "恍惚", "怔怔", "愣神", "发愣", "走神", "木然",
                  "spaced out", "dazed", "lost in thought", "trance"]},
    {"au": "AU43+AU54", "label": "垂眸/垂眼",
     "keywords": ["垂眸", "垂下眼帘", "垂下眼睛", "半垂着眼", "眼帘低垂", "eyes lowered"]},
    # --- 思念 / 惆怅 / 失落 ---
    {"au": "AU1+AU4+AU54", "label": "思念/怀念（眉内侧上抬+垂眸）",
     "keywords": ["思念", "怀念", "想念", "思乡", "乡愁", "追忆", "longing", "nostalgic"]},
    {"au": "AU1+AU4+AU15", "label": "惆怅/怅然",
     "keywords": ["惆怅", "怅然", "怅惘", "落寞", "空落落", "wistful", "forlorn"]},
    {"au": "AU1+AU4+AU15+AU54", "label": "失落/消沉",
     "keywords": ["失落", "消沉", "颓丧", "萎靡", "心灰意冷", "downcast", "crestfallen"]},
    # --- 复合情绪（两种情绪交叠的写法，以前最容易整条漏掉）---
    {"au": "AU1+AU2+AU5+AU12", "label": "又惊又喜（惊+笑叠加）",
     "keywords": ["又惊又喜", "惊喜交加", "喜出望外", "惊喜", "pleasantly surprised", "delighted surprise"]},
    {"au": "AU12+AU15", "label": "哭笑不得",
     "keywords": ["哭笑不得", "啼笑皆非", "不知该哭还是该笑", "not know whether to laugh or cry"]},
    {"au": "AU1+AU4+AU12+AU15", "label": "悲喜交加/百感交集",
     "keywords": ["悲喜交加", "百感交集", "五味杂陈", "感慨万千", "千言万语", "mixed feelings", "bittersweet"]},
    {"au": "AU12+AU15", "label": "强颜欢笑（笑里带悲）",
     "keywords": ["强颜欢笑", "强作欢笑", "强挤出笑", "挤出笑容", "勉强地笑", "forced smile", "fake smile"]},
    {"au": "AU12+AU7", "label": "皮笑肉不笑/假笑",
     "keywords": ["皮笑肉不笑", "假笑", "敷衍地笑", "僵笑", "plastic smile", "false smile"]},
    {"au": "AU12(单侧)+AU7", "label": "似笑非笑",
     "keywords": ["似笑非笑", "半笑不笑", "笑意不达眼底", "笑而不语", "half-smile", "tight smile"]},
    {"au": "AU4+AU12(单侧)", "label": "怒极反笑",
     "keywords": ["怒极反笑", "气极反笑", "气得笑了", "laughing in anger"]},
    {"au": "AU12+AU25+AU6", "label": "忍俊不禁/噗嗤一笑",
     "keywords": ["忍俊不禁", "噗嗤", "扑哧", "憋不住笑", "cant help laughing", "burst into laughter"]},
    {"au": "AU1+AU4+AU15+AU24", "label": "神色复杂/表情复杂",
     "keywords": ["神色复杂", "表情复杂", "神情复杂", "复杂的神情", "难以言喻", "complicated expression"]},
    # --- 愧疚 / 心虚 / 哀求 ---
    {"au": "AU1+AU4+AU54+AU24", "label": "愧疚/自责",
     "keywords": ["愧疚", "自责", "内疚", "抱歉", "歉意", "懊悔", "悔恨",
                  "guilty", "remorseful", "regretful", "ashamed"]},
    {"au": "AU7+AU57/AU58", "label": "心虚/躲闪（眼神飘开）",
     "keywords": ["心虚", "躲闪", "眼神闪躲", "不敢对视", "移开视线", "guilty look", "shifty"]},
    {"au": "AU25+AU17", "label": "哀求/恳求",
     "keywords": ["哀求", "恳求", "央求", "乞求", "求求", "begging", "pleading", "imploring"]},
    # --- 期待 / 欣慰 / 兴奋 ---
    {"au": "AU2+AU5+AU12", "label": "期待/憧憬",
     "keywords": ["期待", "憧憬", "盼望", "神往", "眼前一亮", "hopeful", "expectant"]},
    {"au": "AU6+AU12+AU43(半闭)", "label": "欣慰",
     "keywords": ["欣慰", "满足地", "心头一暖", "gratified", "warm satisfaction"]},
    {"au": "AU12+AU6+AU5", "label": "眼睛发亮/眼中有光",
     "keywords": ["眼睛发亮", "眼中有光", "双眼放光", "眼睛一亮", "eyes lit up", "sparkling eyes"]},
    {"au": "AU5+AU2+AU26", "label": "兴奋/亢奋",
     "keywords": ["兴奋", "亢奋", "激动", "雀跃", "兴致勃勃", "excited", "thrilled", "elated"]},
    # --- 癫狂 / 阴冷 / 讥讽 ---
    {"au": "AU5+AU12+AU27", "label": "癫狂/疯狂大笑",
     "keywords": ["癫狂", "狂笑", "歇斯底里", "发疯", "hysterical", "maniacal", "crazed"]},
    {"au": "AU7+AU4+AU14", "label": "阴冷/邪魅",
     "keywords": ["阴冷", "邪魅", "阴鸷", "森然", "阴森", "sinister", "dark smirk"]},
    {"au": "AU12(单侧)+AU14+AU7", "label": "讥讽/嘲讽",
     "keywords": ["讥讽", "嘲讽", "嗤笑", "哂笑", "讥笑", "scoff", "mock", "derisive"]},
    {"au": "AU2+AU12(单侧)+AU53", "label": "挑衅/张扬",
     "keywords": ["挑衅", "示威", "桀骜", "张狂", "provocative", "defiant", "cocky"]},
    # --- 补充微动作 / 状态 ---
    {"au": "AU1+AU4+AU15+AU25", "label": "哽咽/声音发颤",
     "keywords": ["哽咽", "嗓音发颤", "声音发颤", "泣不成声", "choked up", "voice trembling"]},
    {"au": "AU6+AU25", "label": "叹气/轻叹",
     "keywords": ["叹气", "轻叹", "长叹", "叹了口气", "sigh", "sighing"]},
    {"au": "AU17+AU24", "label": "撇嘴/不以为然",
     "keywords": ["撇嘴", "撇了撇嘴", "不以为然", "嗤之以鼻", "dismissive", "scoffing"]},
    {"au": "AU25+AU26+AU27", "label": "倒吸一口气/张口结舌",
     "keywords": ["倒吸一口气", "倒抽冷气", "张口结舌", "结结巴巴", "gasp", "flabbergasted"]},
    {"au": "无AU激活+AU43(半闭)", "label": "麻木/漠然",
     "keywords": ["麻木", "漠然", "无动于衷", "空洞的眼神", "empty stare", "apathetic"]},
    {"au": "AU7+AU4+AU24", "label": "审视/打量",
     "keywords": ["打量", "端详", "审视着", "上下打量", "scrutinize", "sizing up"]},
]

# ---------- 微表情控制小贴士（参考《80个表情包AI提示词》方法论，已剔除脸型/外貌内容） ----------
AU_TIPS = [
    "克制写法：加 subtle / slight / barely 可抑制卡通化崩坏（实测提升稳定性）",
    "单段聚焦：一条提示词最多 3-5 个肌群、AU 不超过 6 个",
    "真笑看眼角：只有 AU12 没有 AU6 的笑容易假，眼角鱼尾纹是真心笑的标志",
    "情绪强度靠时间窗口拆：0-0.5s 起势、0.5-1.2s 到峰值，每窗 2-4 秒",
    "AU 编码仅作表情分析参考，豆包/视频模型不直接识别，可用于人工校对或表情驱动工具",
]

# AU 编码提取（用于汇总 au_string）
import re
_AU_CODE_RE = re.compile(r"AU\d+")

# 叙事基调短语（描述故事风格，不是面部表情），匹配前先剔除，避免 style_bible 里的
# "自嘲式幽默" 之类写法让所有提示词都误命中同一个表情
_TONE_STRIP_RE = re.compile(r"自嘲[式的]?(?:幽默|喜感|喜剧|段子|调侃|玩笑)")


def extract_aus(text: str) -> dict:
    """从提示词中提取表情/动作关键词并映射到 AU。

    返回:
        {
            "matches": [{"au": "AU12+AU6", "label": "...", "keywords": ["大笑", ...]}, ...],
            "au_string": "AU12+AU6+AU25",   # 去重汇总，可直接给表情驱动工具用
            "source": "lexicon",
        }
    """
    if not text or not text.strip():
        return {"matches": [], "au_string": "", "source": "lexicon"}

    # 先剔除叙事基调短语（如 style_bible 里的"自嘲式幽默"），再做表情匹配
    t = _TONE_STRIP_RE.sub("", text.lower())

    # 1) 先收集全部候选命中
    cands = []
    for item in AU_SETS:
        matched = [kw for kw in item["keywords"] if kw.lower() in t]
        if matched:
            cands.append((item, matched))

    # 2) 长词吃掉短词：「坐立不安」命中了，就不再单独算一条「不安」，
    #    否则一句话会被拆成一堆重复的短碎词，看着像"分析重复"而不是"分析不全"
    all_kw = {kw for _, ms in cands for kw in ms}
    def _covered(kw: str) -> bool:
        return any(kw != o and kw in o for o in all_kw)

    hits, seen_key = [], set()
    max_kw = 6        # 每条最多展示 6 个命中词（一句话里常常写了好几个同义表达）
    max_groups = 14   # 最多展示 14 组（用户反馈"只分析到一部分"，不要再截太狠）
    for item, matched in cands:
        kept = [kw for kw in matched if not _covered(kw)]
        if not kept:
            continue
        key = (item["au"], item["label"])
        if key in seen_key:      # 词库扩充后可能存在同 AU 同标签的重复条目
            continue
        seen_key.add(key)
        hits.append({
            "au": item["au"],
            "label": item["label"],
            "keywords": kept[:max_kw],
            "_score": len(kept),
        })

    # 命中词多的排前面（长词/精确词优先展示），再按顺序截断
    hits.sort(key=lambda x: (-x["_score"], len(x["label"])))
    hits = hits[:max_groups]
    for h in hits:
        h.pop("_score", None)

    # 汇总去重的标准 AU 编码（"头部动作（无标准AU）"等说明性条目不参与汇总）
    codes, seen = [], set()
    for h in hits:
        for code in _AU_CODE_RE.findall(h["au"]):
            if code not in seen:
                seen.add(code)
                codes.append(code)

    return {"matches": hits, "au_string": "+".join(codes), "source": "lexicon"}

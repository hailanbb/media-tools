"""MoviePilot 订阅清理决策引擎 — 基于媒体类型（电影 vs 电视剧）分离的差异化退订、CD2 实盘对账与安全保护算法。

严格对齐：
- 《MediaFlow剧集订阅智能清理需求与交互方案-v2.5.65.md》
- 《MediaFlow电影订阅智能清理需求与交互方案-v2.5.65.md》
- v2.5.63 升级：CD2 实盘准星、多维微短剧识别、断更停滞双分桶（零进度死种 vs 部分已下可补缺）、低质/异常特征识别（无封面/繁体/小语种/无中文名）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SubscriptionDiagnosis:
    """单个订阅的诊断结果。"""

    sub_id: int
    name: str
    year: str
    media_type: str  # "tv" | "movie"
    season: Optional[int]
    total_episodes: Optional[int]
    completed_episodes: Optional[int]
    lack_episodes: Optional[int]
    state: str  # "R" (运行中) | "P" (待定) | "S" (暂停)
    username: str

    # 算法评估
    clean_eligible: bool  # 是否符合清理条件
    clean_confidence: str  # "high" (安全可退订) | "medium" (建议退订/复核) | "low" (保护不可退订)
    clean_rule: str  # completed / stalled / zombie / upcoming_protect / short_tv_stalled / low_quality / protected / target_pending / manual_preserve / airing_protect / active
    clean_rule_name: str  # 规则名称，如 "全季已齐完结"、"电影已入库完成"、"待映/院线窗口期保护"
    clean_reason: str  # 详细判断依据
    days_inactive: int  # 沉寂天数
    created_days_ago: int  # 创建距今天数
    is_protected: bool  # 是否受白名单或待映期绝对保护
    is_short_tv: bool = False  # 是否为短剧/微短剧特征
    is_douban_sync: bool = False  # 是否为豆瓣榜单自动同步
    release_stage: Optional[str] = None  # 电影发行阶段: unannounced | upcoming_theatrical | in_theaters | digital_pending | released
    in_library_status: Optional[str] = None  # 入库状态: in_library | not_in_library | unknown
    version_target_satisfied: Optional[bool] = None  # 目标版本是否满足

    # v2.5.63 扩展治理维度（全部附带默认值，100% 向下兼容）
    short_drama_reason: Optional[str] = None  # 短剧判定依据
    stalled_kind: Optional[str] = None  # "never_downloaded" | "partial_stalled" | "old_no_source" | None
    can_gap_fill: bool = False  # 是否支持联动补缺下载模块筛查/补缺
    is_low_quality: bool = False  # 是否命中低质/异常特征（无封面、繁体、小语种、纯外文、垃圾词条）
    low_quality_flags: List[str] = field(default_factory=list)
    low_quality_reasons: List[str] = field(default_factory=list)

    # CD2 实盘库存真相字段
    data_source: str = "mp"  # "cd2" | "mp"
    cd2_verified: bool = False
    cd2_in_library: Optional[bool] = None
    cd2_source_episodes: Optional[int] = None
    cd2_archive_episodes: Optional[int] = None
    cd2_total_episodes: Optional[int] = None
    cd2_episode_numbers: List[int] = field(default_factory=list)
    cd2_paths: List[str] = field(default_factory=list)
    mp_completed_episodes: Optional[int] = None


PROTECTED_KEYWORDS = re.compile(r"(?:洗版|收藏|keep|留存|4k原盘|原盘|diy)", re.IGNORECASE)

# ── 小语种 / 非中文脚本检测正则 ─────────────────────────────────
# 日文平假名/片假名、长音符、韩文谚文、西里尔(俄文)、阿拉伯文、泰文、天城文(印地语)、希伯来文、希腊文
MINOR_SCRIPT_RE = re.compile(
    r"[\u3040-\u309f\u30a0-\u30ff\u30fc\uac00-\ud7af\u1100-\u11ff"
    r"\u0400-\u04ff\u0600-\u06ff\u0e00-\u0e7f\u0900-\u097f\u0590-\u05ff\u0370-\u03ff]"
)
CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
ALPHA_CHAR_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")

# 明显垃圾/占位词条检测
JUNK_TITLE_RE = re.compile(
    r"(?:重复词条|重复条目|^\s*(?:test|testing|tbd|untitled|unknown|null|none|测试)\s*$)",
    re.IGNORECASE,
)

# 精选高频「纯繁体独有字」集合（严格排除简体规范通用字，避免对简体标题误报）
TRADITIONAL_ONLY_CHARS = frozenset(
    "萬與專業叢東絲兩嚴喪個豐臨為麗舉義烏樂喬習鄉書買亂爭於虧雲亞產親億僅從侖倉儀們價眾優夥會傘偉傳傷倫偽佇體餘佛作傭偵側僑傑備傢傷傾僅動務勝勞勢勳勻區醫華協單賣衛卻廠廳歷厲壓厭廁廂廈廚廝嚴縣參雙發變敘疊葉號嘆嚇呂嗎君吟否吧吳吶吸吹吻吼呀呆呈告員嗚呢周味呵呼命咀咂咖響啞啟啡銜啤啥啦啪喀喂善喇喊喔喘喜喝喧喪喬單嗎嗅嗆嗇嗜嘉嘔嘗嘛嘟嘩嘯嘲嘴嘶撫嘻嘿器噩噪噬噴噸噹嚇嚴囉囊"
    "國圖團圓園圍國聖場塊堅壇壓壘壞壟壢壺壽夢夠奪獎奮奧婦媽嬌嬰學寧實審寫寬寵寶尋導將專對爾塵堯尷屍層屜屬歲島嶺嶽峽巒嶄嶼巔幣帥師帳帶幀幫幕幟權條來楊傑極構槍榮樂樓標樞樣樸樹橋機橫檔檢檯櫃欄權歡歐殲毀畢氣漢湯溝溫滄滅滌滬滯滲滿漁漆演漢漫漸潔潛潮澤濃濕濟濤濫濱濺濾瀉瀋瀏瀑瀕爐爛爭爺爾牆牘牽犧狀狹狽猛猶獨獄獵獸獻現瑪環璽瓊電畫暢疇疊療癒發皚皺監盤盧盪盡盲直眾睜睞督瞞矚瞭礙礦碼磚礎磯禮社祕禱禍禪離種稱穀積穎穩競筆築篩簡簽簾籃籌籍籠籤米類粵糧糰緊紅納紙級紛素索組細終結絕絞絡給統絲經綜綠維綱網綴綵綸綺緊緒線締緣編緩緬緯練縛縣縮縱總績繁繃縫縮織繞繪繳繹繼續纏纖纜"
    "羅罰罵罷義習翻翼耀老聯聰聲職聽肅脅脈腳脫臉臘臨臺與興舉舊艙艦艱艷藝節芻苑范茲荊莊莖華菲萊萬落葉著葛葡董葦葬蔥葵葷蒂蒙蒜蒞蒼蓄蓋蓮蓯蔭蔔藍藝藥蘊蘆蘋蘭處虛號虧蟲蝕螢衛衝補裝裏製複褲襲見規視覺覽觀觸計訂訃認討讓記訛訝許論設訪訣證評詛識詐訴診詞詔譯試詩詰誠誅話誕詢詣該詳詫誇誌認誑誓誘語誠誡誣誤誥誦誨說誰課誼調諂談請諍諒論諦諧諫諭諮諱諷諸諺諾謀謁謂謄謊謎謐謗謙講謝謠謨謫謬謳謹證譏識譙譚譜警譯議護譽讀變讓讒讖讚"
    "貝貞負財貢貧貨販貪貫責貯貴貶買貸費貼賀貽貿賀賃賄資賈賊賑賓賜賞賠賢賣賤賦質賬賭賴賺購賽贈贊贏贖趨躍躋軍軒軟較載輔輕輛輝輩輪輯輸轉轍轟辦辭辯農運過達違遙遜遞遠適遭遮遲遷選遺遼邁還邇邊邏鄧鄭鄰鬱醫釀釋釐鍾鐵鋼錄錢錦錫錯鍋鍵鍾鍛鎮鏡鐘鑑鑒長門閃閉開閒間閔閘閣閥閨閩閱閹閻閼闊闌闈闔闖關闡闢防衛陽陰陣階際陸陳險隨隱隸隻雄雅集雇雉雋雌雍雕雖雙雜雞離難雲電需震霍霧露霸靂靈靜響頁頂頃項順須頌預頑頒頓頗領頜頡頤頭頰頸頹頻顆題額顏願顛類顧顯風飄飛饑飯飲飽飾餃餅養餐餘館首香馬馭馮馳馴駁駐駕駛駝駭駱駿騎騙騷驅驗驚驛驟驢骨體高髮鬧鬱鬼魅魔魚魯鮮鯉鯊鯨鱷鳥鳴鳳鴉鴨鴻鴿鵝鶴鷹鸚鹽麥麵麻黃點黨黯鼓齊齒齡龍龐龜"
    "臺臺灣盤點後開關門問間時間長車馬風雲電飛愛樂書畫聽說讀寫買賣貴錢銀鐵銅錄錯離難雜雙體頭顏飯飲館驚驗髮鬧閉閒聞閣閥閱關闡際陸陳陽陰階隊隨險隱隻雄雅集雇雕雖雞電需震霍霧露霸靜頁頂項順須頑顧頓頒預領頗頸頻顆題額顏願類顯風飛食飯飽飾餅養餐館首馬駐駕驗驚骨髮鬱魚鮮鳥鳴鳳鴨鴻鵝點黨齊龍龜"
)
# 移除任何可能出现在简体常用字表中的字符（用 GB2312 简体编码双向安全过滤 + 手动白名单）
_SIMPLIFIED_SAFE_EXCLUSIONS = frozenset(
    "作佛佣侧仅动务劳匀区协单卫却厂历压厌参双发变叶号叹吓吕吗君吟否吧吴呐吸吹吻吼呀呆呈告员呜呢周味呵呼命咀咂咖响哑启啡衔啤啥啦啪喀喂善喇喊喔喘喜喝喧丧乔单嗅呛啬嗜嘉呕尝嘛嘟哗啸嘲嘴嘶抚嘻嘿器噩噪噬喷吨当囊"
    "国图团圆园围圣场块坚坛垒坏垄壶寿梦够夺奖奋奥妇妈娇婴学宁实审写宽宠宝寻导将专对尔尘尧尴尬尸层屉属岁岛岭岳峡峦崭屿巅币帅师帐带帧帮幕帜权条来杨杰极构枪荣乐楼标枢样朴树桥机横档检台柜栏欢欧歼毁毕气汉汤沟温沧灭涤沪滞渗满渔漆演漫渐洁潜潮泽浓湿济涛滥滨溅滤泻沈浏瀑濒炉烂争爷墙牍牵牺状狭狈猛犹独狱猎兽献现玛环玺琼电画畅畴叠疗愈皑皱监盘卢荡尽盲直众睁睐督瞒瞩了碍矿码砖础矶礼社秘祷祸禅离种称谷积颖稳竞笔筑筛简签帘篮筹籍笼米类粤粮团紧红纳纸级纷素索组细终结绝绞络给统丝经综绿维纲网缀彩纶绮绪线缔缘编缓缅纬练缚县缩纵总绩繁绷缝织绕绘缴绎继续缠纤缆"
    "罗罚骂罢义习翻翼耀老联聪声职听肃胁脉脚脱脸腊临台与兴举旧舱舰艰艳艺节刍苑范兹荆庄茎华菲莱万落叶著葛葡董苇葬葱葵荤蒂蒙蒜莅苍蓄盖莲苁荫卜蓝药蕴芦苹兰处虚亏虫蚀萤卫冲补装里制复裤袭见规视觉览观触计订讣认讨让记讹讶许论设访诀证评诅识诈诉诊词诏译试诗诘诚诛话诞询诣该详诧夸志诳誓诱语诫诬误诰诵诲说谁课谊调谄谈请诤谅谛谐谏谕咨讳讽诸谚诺谋谒谓誊谎谜谧谤谦讲谢谣谟谪谬讴谨讥谯谭谱警议护誉读谗谶赞"
    "贝贞负财贡贫货贩贪贯责贮贵贬买贷费贴贺贻贸赁贿资贾贼赈宾赐赏赔贤卖贱赋质账赌赖赚购赛赠赞赢赎趋跃跻军轩软较载辅轻辆辉辈轮辑输转辙轰办辞辩农运过达违遥逊递远适遭遮迟迁选遗辽迈还迩边逻邓郑邻郁医酿释厘钟铁钢录钱锦锡错锅键锻镇镜鉴长门闪闭开闲间闵闸阁阀闺闽阅阉阎阏阔阑闱阖闯关阐辟防阳阴阵阶际陆陈险随隐隶只雄雅集雇雉隽雌雍雕虽双杂鸡难云需震霍雾露霸雳灵静页顶顷项顺须颂预顽颁顿颇领颌颉颐头颊颈颓频颗题额颜愿颠类顾显风飘飞饥饭饮饱饰饺饼养餐余馆首香马驭冯驰驯驳驻驾驶驼骇骆骏骑骗骚驱验惊驿骤驴骨体高发闹鬼魅魔鱼鲁鲜鲤鲨鲸鳄鸟鸣凤鸦鸭鸿鸽鹅鹤鹰鹦盐麦面麻黄点党黯鼓齐齿龄龙庞龟"
    "后点开关门问间时间长车马风爱书画听说读写买贵银铜"
    "一二三四五六七八九十百千万亿中大小上下左右前后内外高低长短新旧好坏多少美丑真假黑白红黄蓝绿青紫金木水火土日月星辰天地人你我他她它们的是在有了不这那和与或但因为所以如果虽然可是就是可以能够应该必须需要想要觉得知道看见听见说话做事工作学习生活朋友家人爸爸妈妈哥哥姐姐弟弟妹妹儿子女儿老师学生医生警察英雄世界国家城市乡村学校医院公司电影电视剧集动漫综艺纪录片"
    "乾坤于了云亚产亩享京亮亲亿仅仆从仑仓仪们价众优伙会伞伟传伤伦伪伫体余佛佣侦侧侨杰备家倾动务胜劳势勋匀区医华协单卖卫却厂厅历厉压厌厕厢厦厨厮严县参双发变叙叠叶号叹吓吕吗君吟否吧吴呐吸吹吻吼呀呆呈告员呜呢周味呵呼命咀咂咖响哑启啡衔啤啥啦啪喀喂善喇喊喔喘喜喝喧丧乔嗅呛啬嗜嘉呕尝嘛嘟哗啸嘲嘴嘶抚嘻嘿器噩噪噬喷吨当囊"
    "重里只准制表复干后游系范松郁苏咸面须钟谷采板折卷冲划别斗历只台号"
)
# 纯繁体独有字集：从候选集中严格扣除所有简体安全字及 GB2312 一级常用简体字，确保零简体误报
def _is_gb2312_level1(ch: str) -> bool:
    try:
        raw = ch.encode("gb2312")
        return len(raw) == 2 and 0xB0 <= raw[0] <= 0xD7
    except UnicodeEncodeError:
        return False


PURE_TRADITIONAL_CHARS = frozenset(
    ch for ch in (TRADITIONAL_ONLY_CHARS - _SIMPLIFIED_SAFE_EXCLUSIONS)
    if not _is_gb2312_level1(ch)
)

# ── 短剧/微短剧多维识别规则（借鉴 GitHub PlayletCategory / TMDB / 豆瓣最佳实践） ──
# 显式排除的 TMDB 类型 ID：16=动画, 99=纪录片, 10764=真人秀/综艺, 10767=脱口秀, 10763=新闻, 10762=儿童
SHORT_DRAMA_EXCLUDED_GENRE_IDS = frozenset({16, 99, 10762, 10763, 10764, 10767})
SHORT_DRAMA_EXCLUDED_TITLE_RE = re.compile(
    r"(?:纯享版|大神版|加更版|完整版|未播|晚会|盛典|演唱会|春晚|跨年|纪录片|公开课|发布会|颁奖礼|花絮)",
    re.IGNORECASE,
)
SHORT_DRAMA_EXPLICIT_RE = re.compile(
    r"(?:微短剧|短剧|竖屏|小程序剧|红果短剧|抖音短剧|快手短剧|playlet|mini[\s-]?drama|short[\s-]?drama)",
    re.IGNORECASE,
)
# 高置信度中文微短剧高频题材词（仅与 S01 + 高集数/99集 联合触发，杜绝单独触发误杀常规剧）
SHORT_DRAMA_TROPE_RE = re.compile(
    r"(?:重生|穿越|闪婚|退婚|离婚后|霸总|总裁|战神|龙王|赘婿|神医|千金|萌宝|逆袭|首富|神豪|"
    r"皇叔|替嫁|错嫁|团宠|马甲|收废品|棺中醒来|醒来后|跪求|休妻|真假千金|下山|全家读心|读心术|"
    r"八零|九零|七零|太奶|娇妻|甜宠|疯批|白月光|追妻|悔婚|断亲|高嫁|悍妻|弃妃|毒妃|医妃|王妃|"
    r"庶女|嫡女|农女|开局|天尊|仙尊|帝尊|魔尊|满级|女帝|大佬|奶爸|萌娃|萌妻|宠妻|前夫|前妻|"
    r"渣男|绿茶|恶毒女配|炮灰|绑定系统|签到|神算|相亲|闪恋|豪门|隐婚|逼婚|逃婚|带球跑|假千金)"
)


def detect_low_quality_signals(sub: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """检测订阅条目是否存在大概率需清理的低质/异常特征。

    返回: (flags, human_reasons)
    注意：仅当字典中显式存在 poster/backdrop 键（真实 MP 订阅均包含）且全为空时才判定无封面，
    避免纯单元测试构造的最小字典发生误判。
    """
    flags: List[str] = []
    reasons: List[str] = []
    name = str(sub.get("name") or sub.get("title") or "").strip()

    # 1. 无封面图片
    if "poster" in sub or "backdrop" in sub:
        poster = str(sub.get("poster") or "").strip()
        backdrop = str(sub.get("backdrop") or "").strip()
        if not poster and not backdrop:
            flags.append("no_poster")
            reasons.append("无封面图")

    # 2. 垃圾/重复占位词条
    if name and JUNK_TITLE_RE.search(name):
        flags.append("junk_title")
        reasons.append("疑似垃圾/占位词条")

    # 3. 小语种文字（日文假名、韩文谚文、俄文、泰文、阿拉伯文等）
    if name and MINOR_SCRIPT_RE.search(name):
        flags.append("minor_script")
        reasons.append("小语种标题")

    # 4. 繁体字标题（命中纯繁体独有字）
    if name:
        trad_hits = [ch for ch in name if ch in PURE_TRADITIONAL_CHARS]
        if trad_hits:
            flags.append("traditional_chinese")
            sample = "".join(dict.fromkeys(trad_hits))[:4]
            reasons.append(f"繁体字标题({sample})")

    # 5. 纯外文 / 无中文译名（无任何汉字但有拉丁字母）
    if name and not CJK_CHAR_RE.search(name) and ALPHA_CHAR_RE.search(name):
        if "minor_script" not in flags:
            flags.append("no_chinese_title")
            reasons.append("无中文译名")

    return flags, reasons


def detect_short_drama(
    sub: Dict[str, Any],
    name: str,
    season: Optional[int],
    total_ep: Optional[int],
    comp_ep: int,
) -> Tuple[bool, Optional[str]]:
    """多维融合短剧/微短剧判定引擎（借鉴 MoviePilot PlayletCategory + TMDB + 开源社区实践）。

    优先级与防误杀原则：
    1. 硬白名单排除：动漫(genre 16 / CD2 动漫路径)、纪录片(99)、综艺/真人秀(10764/10767)、儿童(10762)、
       含综艺/晚会后缀，或 S02+ 多季长寿剧（除非显式标注短剧）—— 严防误杀《仙逆》等国漫年番与综艺。
    2. 显式标记/标题/简介/标签：明确包含“短剧/微短剧/竖屏/playlet”。
    3. TMDB 单集时长（金标准）：episode_run_time 存在且 1 <= runtime <= 15 分钟（华语区或中文标题）。
    4. 结构 + 强微短剧命名特征：S01 + (total_ep == 99 或 >= 40) + 命中中文微短剧高频题材词。
    5. CD2 实物单集均体积佐证：CD2 实存 >= 10 集且非 strm 均体积 < 65 MB。
    """
    # 显式调用方强制指定
    if sub.get("is_short_drama") is True:
        return True, "显式标记为短剧/微短剧"

    # 显式标题/简介/标签关键词
    overview = str(sub.get("overview") or sub.get("description") or "")
    if SHORT_DRAMA_EXPLICIT_RE.search(name):
        return True, "标题明确含短剧/微短剧标识"
    if overview and SHORT_DRAMA_EXPLICIT_RE.search(overview):
        return True, "简介明确含短剧/微短剧标识"

    # ── 硬排除白名单（严防误杀动漫年番、综艺、纪录片、多季正剧）──
    genre_ids = set()
    raw_genres = sub.get("tmdb_genre_ids") or sub.get("genre_ids") or []
    for g in raw_genres:
        try:
            genre_ids.add(int(g))
        except (TypeError, ValueError):
            pass
    if genre_ids & SHORT_DRAMA_EXCLUDED_GENRE_IDS:
        return False, None

    cd2_paths = sub.get("cd2_paths") or []
    if any("/动漫/" in str(p) or "/儿童/" in str(p) for p in cd2_paths):
        return False, None

    if SHORT_DRAMA_EXCLUDED_TITLE_RE.search(name):
        return False, None

    # 真正的网络微短剧极少超过 S01；S02+ 通常是正剧/长寿综艺/续集动漫
    if season is not None and season >= 2:
        return False, None

    # ── 证据 1：TMDB 单集时长（开源社区公认最准确依据）──
    runtimes = sub.get("tmdb_episode_run_time") or sub.get("episode_run_time") or []
    valid_rts = [int(r) for r in runtimes if isinstance(r, (int, float)) and 0 < int(r) <= 180]
    if valid_rts:
        min_rt = min(valid_rts)
        origin_countries = {str(c).upper() for c in (sub.get("tmdb_origin_country") or sub.get("origin_country") or [])}
        is_cn_or_cjk = bool(origin_countries & {"CN", "HK", "TW"}) or bool(CJK_CHAR_RE.search(name))
        if min_rt <= 15 and is_cn_or_cjk:
            return True, f"TMDB 单集时长仅 {min_rt} 分钟（符合微短剧/短剧特征）"

    # ── 证据 2：S01 + 虚标 99 / 高集数(>=40) + 中文微短剧题材命名 ──
    high_ep = (total_ep == 99) or ((total_ep or 0) >= 40) or (comp_ep >= 40)
    if high_ep and CJK_CHAR_RE.search(name):
        trope_m = SHORT_DRAMA_TROPE_RE.search(name)
        if trope_m:
            ep_label = "虚标99集" if total_ep == 99 else f"高集数({total_ep or comp_ep}集)"
            return True, f"S01 {ep_label}且命中微短剧常见题材词「{trope_m.group(0)}」"

    # ── 证据 3：CD2 实存多集且单集平均体积极小（非 strm）──
    avg_mb = sub.get("cd2_avg_video_mb")
    cd2_cnt = sub.get("cd2_total_episodes") or 0
    if isinstance(avg_mb, (int, float)) and avg_mb > 0 and avg_mb < 65 and cd2_cnt >= 10:
        return True, f"CD2 实存 {cd2_cnt} 集单集均体积仅 {avg_mb:.1f}MB（微短剧短片特征）"

    return False, None


def _parse_days(date_str: Optional[str], last_up_str: Optional[str], now: datetime) -> Tuple[int, int]:
    """解析创建天数与沉寂天数。"""
    created_days = 0
    if date_str:
        try:
            dt = datetime.strptime(str(date_str)[:19], "%Y-%m-%d %H:%M:%S")
            created_days = max(0, (now - dt).days)
        except Exception:
            pass

    inactive_days = 0
    if last_up_str:
        try:
            dt = datetime.strptime(str(last_up_str)[:19], "%Y-%m-%d %H:%M:%S")
            inactive_days = max(0, (now - dt).days)
        except Exception:
            pass
    elif created_days > 0:
        inactive_days = created_days

    return created_days, inactive_days


# ─────────────────────────────────────────────────────────────
# 1. 🎬 电影（Movie）专属清理决策算法
# ─────────────────────────────────────────────────────────────
def diagnose_movie_subscription(sub: Dict[str, Any], now: Optional[datetime] = None) -> SubscriptionDiagnosis:
    """对【电影】订阅进行专属事实诊断与清理决策（以 CD2 实盘库存为最高准星）。"""
    if now is None:
        now = datetime.now()

    sub_id = int(sub.get("id") or sub.get("sub_id") or 0)
    name = str(sub.get("name") or sub.get("title") or "未知")
    year_str = str(sub.get("year") or "").strip()
    username = str(sub.get("username") or "admin")
    state = str(sub.get("state") or "R").upper()
    best_ver = int(sub.get("best_version") or 0) == 1 or int(sub.get("best_version_full") or 0) == 1
    custom_words = str(sub.get("custom_words") or "")

    created_days, inactive_days = _parse_days(sub.get("date"), sub.get("last_update"), now)
    is_douban = (username == "豆瓣榜单")

    # 低质/异常特征检测（无封面、繁体、小语种、纯外文、垃圾词条）
    lq_flags, lq_reasons = detect_low_quality_signals(sub)
    is_low_quality = len(lq_flags) > 0

    movie_year = int(year_str) if year_str.isdigit() else 0

    # CD2 实盘真相优先（3.1：已入库完成判定以 CD2 为准）
    cd2_verified = bool(sub.get("cd2_verified"))
    cd2_in_library = sub.get("cd2_in_library")  # True | False | None
    cd2_paths = list(sub.get("cd2_paths") or [])
    data_source = "cd2" if cd2_verified else "mp"

    # 兼容既有单元测试的 in_library / not_in_library 注入；当 cd2_verified=True 时严格以 CD2 为准
    if cd2_verified:
        in_library_evidence = cd2_in_library is True
        not_in_library = cd2_in_library is False
    else:
        in_library_evidence = sub.get("in_library")
        not_in_library = sub.get("not_in_library") is True

    release_stage = sub.get("release_stage")
    has_target_version = sub.get("has_target_version")
    digital_date = sub.get("digital_date")
    theatrical_date = sub.get("theatrical_date")

    lib_status_str = "in_library" if in_library_evidence else ("not_in_library" if not_in_library else "unknown")

    common_ext = dict(
        is_low_quality=is_low_quality,
        low_quality_flags=lq_flags,
        low_quality_reasons=lq_reasons,
        data_source=data_source,
        cd2_verified=cd2_verified,
        cd2_in_library=cd2_in_library if cd2_verified else None,
        cd2_paths=cd2_paths,
    )

    # 门禁 M1：洗版与留存白名单保护
    if best_ver:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=1 if in_library_evidence else 0, lack_episodes=None,
            state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="电影洗版监控中",
            clean_reason="开启了最佳版本/洗版监控，持续守候更高画质（4K REMUX/原盘），严禁退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_douban_sync=is_douban, release_stage=release_stage,
            in_library_status=lib_status_str, **common_ext,
        )

    if PROTECTED_KEYWORDS.search(custom_words) or PROTECTED_KEYWORDS.search(name):
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=1 if in_library_evidence else 0, lack_episodes=None,
            state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="收藏白名单保护",
            clean_reason="命中用户收藏/留存关键字，禁止退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_douban_sync=is_douban, release_stage=release_stage,
            in_library_status=lib_status_str, **common_ext,
        )

    # 门禁 M2：实物已核实入库（CD2 已验证时严格以 CD2 为准；未同步 CD2 时兼容旧证据）
    is_in_library = bool(in_library_evidence) or (
        not cd2_verified
        and state == "P"
        and not not_in_library
        and release_stage not in ("in_theaters", "upcoming_theatrical")
    )

    if is_in_library and not not_in_library:
        if has_target_version is False or sub.get("version_target_satisfied") is False:
            return SubscriptionDiagnosis(
                sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
                total_episodes=1, completed_episodes=1, lack_episodes=0, state=state, username=username,
                clean_eligible=False, clean_confidence="low",
                clean_rule="target_pending", clean_rule_name="目标版本仍待满足",
                clean_reason="媒体库已有其他版本，但用户指定的目标版本（4K/原盘/音轨）仍待满足，继续保留",
                days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
                is_douban_sync=is_douban, release_stage=release_stage,
                in_library_status="in_library", version_target_satisfied=False, **common_ext,
            )

        path_hint = f"（CD2 实盘：{cd2_paths[0]}）" if cd2_paths else ""
        reason_text = (
            f"CD2 实盘已核实入库完整视频文件{path_hint}，满足收藏目标，建议安全退订释放订阅"
            if cd2_verified
            else "已核实媒体库中完整文件满足收藏目标，建议安全退订释放订阅"
        )
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=1, lack_episodes=0, state=state, username=username,
            clean_eligible=True, clean_confidence="high",
            clean_rule="completed", clean_rule_name="电影已入库完成(CD2)" if cd2_verified else "电影已入库完成",
            clean_reason=reason_text,
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=is_douban, release_stage=release_stage or "released",
            in_library_status="in_library", version_target_satisfied=True, **common_ext,
        )

    # 门禁 M2.5（v2.5.63 · 3.3）：低质/异常特征自动清理候选（无封面图/垃圾词条/小语种/繁体/纯外文）
    # 仅针对非手动订阅且未入库条目，用户手动添加的保留意图
    if is_low_quality and is_douban:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="low_quality", clean_rule_name="异常特征待清",
            clean_reason=f"命中大概率无效/低质特征（{'、'.join(lq_reasons)}），且未入库，建议复核或批量退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=True, release_stage=release_stage or "unknown",
            in_library_status=lib_status_str,
            stalled_kind="old_no_source" if (movie_year > 0 and movie_year < now.year - 1) else None,
            can_gap_fill=False, **common_ext,
        )

    # 门禁 M3：待映/院线/家庭发行窗口期绝对保护
    is_upcoming_stage = release_stage in ("unannounced", "upcoming_theatrical", "in_theaters", "digital_pending")
    is_recent_year = (movie_year >= now.year) and (release_stage != "released")

    if is_upcoming_stage or is_recent_year:
        if release_stage == "upcoming_theatrical":
            reason_desc = f"影片已定档待上映（定档 {theatrical_date or year_str}），处于院线窗口期"
        elif release_stage == "in_theaters":
            reason_desc = "院线上映已确认，数字发行日期待公布，暂无流媒体资源属正常现象"
        elif release_stage == "digital_pending":
            reason_desc = f"数字版已定档待发售（发售日 {digital_date or '待公布'}），等待家庭发行"
        elif release_stage == "unannounced":
            reason_desc = "影片处于待映前期阶段，公映排期未定，严密锁定保护"
        else:
            year_tag = f"{movie_year}年待映/热映" if movie_year >= now.year else f"{movie_year}年新片"
            reason_desc = f"{year_tag}新片，尚处于院线或家庭发行窗口期，无做种属正常等待，自动保护锁定"

        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="upcoming_protect", clean_rule_name="待映/院线窗口期保护",
            clean_reason=reason_desc,
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_douban_sync=is_douban, release_stage=release_stage or "upcoming",
            in_library_status=lib_status_str, **common_ext,
        )

    # 门禁 M4：往年老片长期无源死种（3.2：支持先用补缺站点能力批量筛查资源再决定下载或退订）
    if movie_year > 0 and movie_year < (now.year - 1) and created_days >= 60:
        if not is_douban and not sub.get("allow_manual_stalled"):
            return SubscriptionDiagnosis(
                sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
                total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
                clean_eligible=False, clean_confidence="low",
                clean_rule="manual_preserve", clean_rule_name="手动收藏意图保留",
                clean_reason=f"用户手动添加的 {movie_year} 年老片，已添加 {created_days} 天，默认保留收藏意图",
                days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
                is_douban_sync=False, release_stage=release_stage or "released",
                in_library_status=lib_status_str,
                stalled_kind="old_no_source", can_gap_fill=True, **common_ext,
            )

        cd2_note = "CD2 核实未入库，" if cd2_verified else ""
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="zombie", clean_rule_name="往年老片无源死种",
            clean_reason=f"{movie_year} 年老片，{cd2_note}订阅已逾 {created_days} 天无入库进展；可先一键筛查站点资源或批量退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=is_douban, release_stage=release_stage or "released",
            in_library_status=lib_status_str,
            stalled_kind="old_no_source", can_gap_fill=True, **common_ext,
        )

    # 门禁 M5：榜单自动同步长期(>=90天)未入库（同属可筛查/退订候选）
    if is_douban and created_days >= 90:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
            total_episodes=1, completed_episodes=0, lack_episodes=1, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="douban_stale", clean_rule_name="长期未下无源",
            clean_reason=f"订阅添加已逾 {created_days} 天无下载进展，可先一键筛查站点资源或批量清理",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_douban_sync=True, release_stage=release_stage or "released",
            in_library_status=lib_status_str,
            stalled_kind="never_downloaded", can_gap_fill=True, **common_ext,
        )

    # 兜底：常规监控中
    return SubscriptionDiagnosis(
        sub_id=sub_id, name=name, year=year_str, media_type="movie", season=None,
        total_episodes=1, completed_episodes=0, lack_episodes=None, state=state, username=username,
        clean_eligible=False, clean_confidence="low",
        clean_rule="active", clean_rule_name="电影追更监控中",
        clean_reason="电影处于正常巡检监控期，暂不执行退订",
        days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
        is_douban_sync=is_douban, release_stage=release_stage or "unknown",
        in_library_status=lib_status_str,
        can_gap_fill=bool(movie_year > 0 and movie_year < now.year), **common_ext,
    )


# ─────────────────────────────────────────────────────────────
# 2. 📺 电视剧（TV Series）专属清理决策算法
# ─────────────────────────────────────────────────────────────
def diagnose_tv_subscription(sub: Dict[str, Any], now: Optional[datetime] = None) -> SubscriptionDiagnosis:
    """对【电视剧】订阅进行专属事实诊断与清理决策（以 CD2 实盘集数为准，精准拆分零进度死种与部分已下可补缺）。"""
    if now is None:
        now = datetime.now()

    sub_id = int(sub.get("id") or sub.get("sub_id") or 0)
    name = str(sub.get("name") or sub.get("title") or "未知")
    year_str = str(sub.get("year") or "").strip()
    season = sub.get("season")
    username = str(sub.get("username") or "admin")
    state = str(sub.get("state") or "R").upper()

    raw_total_ep = sub.get("total_episode") or sub.get("total_episodes")
    tmdb_total_ep = sub.get("tmdb_season_episodes")
    # 若有 TMDB 真实总集数，修正 MP 的 99 占位符
    if tmdb_total_ep and int(tmdb_total_ep) > 0 and (not raw_total_ep or int(raw_total_ep) == 99):
        total_ep: Optional[int] = int(tmdb_total_ep)
    else:
        total_ep = int(raw_total_ep) if raw_total_ep is not None else None

    mp_lack_ep = sub.get("lack_episode")
    if sub.get("completed_episode") is not None or sub.get("current_episode") is not None:
        mp_comp_ep = int(sub.get("completed_episode") or sub.get("current_episode") or 0)
    elif raw_total_ep is not None and mp_lack_ep is not None:
        mp_comp_ep = max(0, int(raw_total_ep) - int(mp_lack_ep))
    else:
        mp_comp_ep = 0

    # CD2 实盘集数优先（2.4：以 CD2 为准）
    cd2_verified = bool(sub.get("cd2_verified"))
    cd2_src_eps = sub.get("cd2_source_episodes")
    cd2_arch_eps = sub.get("cd2_archive_episodes")
    cd2_tot_eps = sub.get("cd2_total_episodes")
    cd2_ep_nums = list(sub.get("cd2_episode_numbers") or [])
    cd2_paths = list(sub.get("cd2_paths") or [])
    data_source = "cd2" if cd2_verified else "mp"

    if cd2_verified and cd2_tot_eps is not None:
        comp_ep = int(cd2_tot_eps)
        if total_ep and total_ep > 0 and total_ep != 99:
            lack_ep: Optional[int] = max(0, total_ep - comp_ep)
        elif total_ep == 99:
            lack_ep = max(0, 99 - comp_ep)
        else:
            lack_ep = None
    else:
        comp_ep = mp_comp_ep
        lack_ep = int(mp_lack_ep) if mp_lack_ep is not None else None

    best_ver = int(sub.get("best_version") or 0) == 1 or int(sub.get("best_version_full") or 0) == 1
    custom_words = str(sub.get("custom_words") or "")

    created_days, inactive_days = _parse_days(sub.get("date"), sub.get("last_update"), now)
    is_douban = (username == "豆瓣榜单")

    season_finished = sub.get("season_finished")
    is_airing = sub.get("is_airing") is True

    # 2.2 多维短剧判定（严格排除国漫年番/综艺/纪录片/多季正剧）
    is_short_drama, short_drama_reason = detect_short_drama(sub, name, season, total_ep, comp_ep)

    # 2.5 低质/异常特征检测
    lq_flags, lq_reasons = detect_low_quality_signals(sub)
    is_low_quality = len(lq_flags) > 0

    in_lib_status = (
        ("in_library" if comp_ep > 0 else "not_in_library")
        if cd2_verified
        else ("in_library" if comp_ep > 0 else "unknown")
    )

    common_ext = dict(
        short_drama_reason=short_drama_reason,
        is_low_quality=is_low_quality,
        low_quality_flags=lq_flags,
        low_quality_reasons=lq_reasons,
        data_source=data_source,
        cd2_verified=cd2_verified,
        cd2_in_library=(comp_ep > 0) if cd2_verified else None,
        cd2_source_episodes=int(cd2_src_eps) if cd2_src_eps is not None else None,
        cd2_archive_episodes=int(cd2_arch_eps) if cd2_arch_eps is not None else None,
        cd2_total_episodes=int(cd2_tot_eps) if cd2_tot_eps is not None else None,
        cd2_episode_numbers=cd2_ep_nums,
        cd2_paths=cd2_paths,
        mp_completed_episodes=mp_comp_ep,
        in_library_status=in_lib_status,
    )

    # 门禁 T1：洗版与关键字白名单保护
    if best_ver:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="剧集洗版监控中",
            clean_reason="用户开启了洗版/最佳版本监控，持续守候整季最高画质，严禁退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_short_tv=is_short_drama, is_douban_sync=is_douban, **common_ext,
        )

    if PROTECTED_KEYWORDS.search(custom_words) or PROTECTED_KEYWORDS.search(name):
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=False, clean_confidence="low",
            clean_rule="protected", clean_rule_name="收藏白名单保护",
            clean_reason="命中用户收藏/留存关键字，禁止退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
            is_short_tv=is_short_drama, is_douban_sync=is_douban, **common_ext,
        )

    # 门禁 T2：全季集数齐备入库（High Confidence 安全清理，以 CD2 实存或完整归档为准）
    is_episodes_full = (
        (lack_ep == 0 and comp_ep > 0)
        or (total_ep is not None and total_ep > 0 and total_ep != 99 and comp_ep >= total_ep)
    )
    if is_episodes_full:
        if is_airing or season_finished is False:
            return SubscriptionDiagnosis(
                sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
                total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
                clean_eligible=False, clean_confidence="low",
                clean_rule="airing_protect", clean_rule_name="连载在播保护",
                clean_reason=f"当前已播集已齐 ({comp_ep}/{total_ep or comp_ep} 集)，但本季仍在排播连载中，锁定保留",
                days_inactive=inactive_days, created_days_ago=created_days, is_protected=True,
                is_short_tv=is_short_drama, is_douban_sync=is_douban, **common_ext,
            )

        src_tag = "CD2 实存" if cd2_verified else "集数"
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=0, state=state, username=username,
            clean_eligible=True, clean_confidence="high",
            clean_rule="completed", clean_rule_name="全季已齐完结",
            clean_reason=f"整季{src_tag}全部已齐 ({comp_ep}/{total_ep or comp_ep} 集)，下载并入库完毕，安全退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=is_short_drama, is_douban_sync=is_douban, **common_ext,
        )

    # 门禁 T2.5（v2.5.63 · 2.5）：低质/异常特征订阅（无封面/垃圾词条/小语种/繁体/纯外文）且零实存进度
    if is_low_quality and comp_ep == 0 and is_douban:
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="low_quality", clean_rule_name="异常特征待清",
            clean_reason=f"命中异常/低质特征（{'、'.join(lq_reasons)}），且当前实存 0 集，大概率为无效词条，建议复核或批量退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=is_short_drama, is_douban_sync=True,
            stalled_kind="never_downloaded", can_gap_fill=False, **common_ext,
        )

    # 门禁 T3：短剧/微短剧虚标长期停滞（Medium Confidence）
    if is_short_drama and (state in ("P", "S") or inactive_days >= 30):
        sd_why = short_drama_reason or "短剧特征"
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="short_tv_stalled", clean_rule_name="短剧虚标停滞",
            clean_reason=f"识别为短剧（{sd_why}），已停滞 {inactive_days} 天（当前 {comp_ep}/{total_ep or '?'} 集），建议核查退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=True, is_douban_sync=is_douban,
            stalled_kind="partial_stalled" if comp_ep > 0 else "never_downloaded",
            can_gap_fill=False, **common_ext,
        )

    # 门禁 T4（2.3 细分桶 A）：部分已下·中途断更停滞（曾经下载过部分集数，后续断更 → 强推荐先一键联动补缺梳理！）
    if (state in ("P", "S") or inactive_days >= 30) and comp_ep > 0 and (lack_ep is None or lack_ep > 0):
        src_tag = "CD2 实存" if cd2_verified else "已收录"
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="stalled", clean_rule_name="部分已下·断更停滞",
            clean_reason=f"{src_tag} {comp_ep}/{total_ep or '?'} 集，已 {inactive_days} 天无新入库；建议先用补缺模块批量梳理可用资源，再决定补缺或退订",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=is_short_drama, is_douban_sync=is_douban,
            stalled_kind="partial_stalled", can_gap_fill=True, **common_ext,
        )

    # 门禁 T5（2.3 细分桶 B）：从未下载·零进度无源死种（自订阅起至今 0 集 → 高概率适合批量退订）
    if comp_ep == 0 and ((created_days >= 45 and inactive_days >= 30) or (state in ("P", "S") and created_days >= 30)):
        return SubscriptionDiagnosis(
            sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
            total_episodes=total_ep, completed_episodes=0, lack_episodes=lack_ep, state=state, username=username,
            clean_eligible=True, clean_confidence="medium",
            clean_rule="zombie", clean_rule_name="零进度·从未下载",
            clean_reason=f"自订阅起已逾 {created_days} 天，至今实存 0 集无任何资源入库；适合批量取消订阅（亦可先筛查站点）",
            days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
            is_short_tv=is_short_drama, is_douban_sync=is_douban,
            stalled_kind="never_downloaded", can_gap_fill=True, **common_ext,
        )

    # 门禁 T6：活跃追更连载保护（Low Confidence 严禁退订）
    act_desc = "剧集尚在连载更新中"
    if inactive_days <= 7:
        act_desc += "（近 7 天内有新集活动）"
    elif inactive_days <= 30:
        act_desc += f"（近 {inactive_days} 天内有活动）"

    return SubscriptionDiagnosis(
        sub_id=sub_id, name=name, year=year_str, media_type="tv", season=season,
        total_episodes=total_ep, completed_episodes=comp_ep, lack_episodes=lack_ep, state=state, username=username,
        clean_eligible=False, clean_confidence="low",
        clean_rule="active", clean_rule_name="活跃连载追更中",
        clean_reason=f"{act_desc}，尚有 {lack_ep if lack_ep is not None else (total_ep - comp_ep if total_ep else '?')} 集待更新入库，严禁清理",
        days_inactive=inactive_days, created_days_ago=created_days, is_protected=False,
        is_short_tv=is_short_drama, is_douban_sync=is_douban, **common_ext,
    )


def diagnose_subscription(sub: Dict[str, Any], now: Optional[datetime] = None) -> SubscriptionDiagnosis:
    """统一调度入口：根据 media_type / mtype / type 自动分流到电影或电视剧策略。"""
    raw_type = str(sub.get("type") or sub.get("mtype") or sub.get("media_type") or "").strip().lower()
    is_movie = (
        raw_type in ("movie", "电影")
        or "电影" in raw_type
        or "movie" in raw_type
        or (sub.get("season") is None and not sub.get("total_episode") and sub.get("year"))
    )
    if is_movie:
        return diagnose_movie_subscription(sub, now=now)
    return diagnose_tv_subscription(sub, now=now)


def evaluate_subscriptions(subscriptions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批量评估订阅列表，支持电影与电视剧精细化分组统计与全量聚合。"""
    now = datetime.now()
    diagnoses: List[SubscriptionDiagnosis] = []

    tv_items: List[SubscriptionDiagnosis] = []
    movie_items: List[SubscriptionDiagnosis] = []

    for s in subscriptions:
        diag = diagnose_subscription(s, now=now)
        diagnoses.append(diag)
        if diag.media_type == "movie":
            movie_items.append(diag)
        else:
            tv_items.append(diag)

    def _calc_stats(items: List[SubscriptionDiagnosis]) -> Dict[str, int]:
        high = sum(1 for x in items if x.clean_confidence == "high")
        med = sum(1 for x in items if x.clean_confidence == "medium")
        low = sum(1 for x in items if x.clean_confidence == "low")
        return {
            "total": len(items),
            "high": high,
            "medium": med,
            "protected": low,
            "clean_eligible": high + med,
        }

    overall_stats = _calc_stats(diagnoses)
    tv_stats = _calc_stats(tv_items)
    movie_stats = _calc_stats(movie_items)

    # 电视剧精细化维度统计
    tv_stats["short_tv_stalled"] = sum(1 for t in tv_items if t.clean_rule == "short_tv_stalled")
    tv_stats["partial_stalled"] = sum(1 for t in tv_items if t.clean_rule == "stalled")
    tv_stats["never_downloaded"] = sum(1 for t in tv_items if t.clean_rule == "zombie")
    tv_stats["low_quality"] = sum(1 for t in tv_items if t.is_low_quality)
    tv_stats["low_quality_cleanable"] = sum(1 for t in tv_items if t.clean_rule == "low_quality")
    tv_stats["gap_fill_candidates"] = sum(1 for t in tv_items if t.can_gap_fill)

    # 电影精细化维度统计
    movie_stats["upcoming_protected"] = sum(1 for m in movie_items if m.clean_rule == "upcoming_protect")
    movie_stats["douban_stale"] = sum(1 for m in movie_items if m.clean_rule == "douban_stale")
    movie_stats["old_zombie"] = sum(1 for m in movie_items if m.clean_rule in ("zombie", "douban_stale"))
    movie_stats["low_quality"] = sum(1 for m in movie_items if m.is_low_quality)
    movie_stats["low_quality_cleanable"] = sum(1 for m in movie_items if m.clean_rule == "low_quality")
    movie_stats["gap_fill_candidates"] = sum(1 for m in movie_items if m.can_gap_fill)

    return {
        "overall": overall_stats,
        "tv": tv_stats,
        "movie": movie_stats,
        "items": diagnoses,
    }

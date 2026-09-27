"""生成通用的「视频下载」快捷指令（未签名 plist），导入时会询问服务器地址和密钥。
用法: python3 make_shortcut.py <输出.shortcut> [服务器地址] [密钥]
  服务器地址、密钥可选：填了就作为导入时的默认值（自用时省得手输）。
签名（需 macOS）: shortcuts sign --mode anyone --input 未签名.shortcut --output 视频下载.shortcut
"""
import plistlib, sys, uuid

OUT = sys.argv[1]
SERVER_DEFAULT = sys.argv[2] if len(sys.argv) > 2 else "https://example.com/abcdef123456"
TOKEN_DEFAULT = sys.argv[3] if len(sys.argv) > 3 else ""
OBJ = "￼"


def uid():
    return str(uuid.uuid4()).upper()


def text(s, vars_at=None):
    """纯文本或带变量的文本；vars_at: {位置: 变量附件}"""
    v = {"string": s}
    if vars_at:
        v["attachmentsByRange"] = {f"{{{pos}, 1}}": att for pos, att in vars_at.items()}
    return {"Value": v, "WFSerializationType": "WFTextTokenString"}


def var(name):
    return {"Type": "Variable", "VariableName": name}


def out(u, name):
    return {"Type": "ActionOutput", "OutputUUID": u, "OutputName": name}


def attach(a):
    return {"Value": a, "WFSerializationType": "WFTextTokenAttachment"}


def dict_items(pairs):
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFKey": text(k), "WFItemType": 0, "WFValue": v} for k, v in pairs]},
        "WFSerializationType": "WFDictionaryFieldValue"}


actions = []


def act(ident, **params):
    u = params.pop("UUID", None) or uid()
    params["UUID"] = u
    actions.append({"WFWorkflowActionIdentifier": "is.workflow.actions." + ident,
                    "WFWorkflowActionParameters": params})
    return u


def ctrl(ident, mode, group, **params):
    # 控制动作也要有自己的 UUID，否则「重复项目」无法关联到所属循环（显示红色、取到空值）
    params.update(WFControlFlowMode=mode, GroupingIdentifier=group, UUID=uid())
    actions.append({"WFWorkflowActionIdentifier": "is.workflow.actions." + ident,
                    "WFWorkflowActionParameters": params})


def if_contains(variable, value, group):
    """文本「包含」判断；变量要强制转为文本，否则 iOS 提示“请为此操作中的每个参数选取值”"""
    variable = dict(variable, Aggrandizements=[{"Type": "WFCoercionVariableAggrandizement",
                                                "CoercionItemClass": "WFStringContentItem"}])
    ctrl("conditional", 0, group, WFCondition=99, WFConditionalActionString=value,
         WFInput={"Type": "Variable", "Variable": attach(variable)})


act("comment", WFCommentActionText="ios-video-dl：在 App 里点分享选本快捷指令，或复制链接后运行。"
    "服务器下载好后自动存到相册。服务器地址和密钥在服务器上执行 vdlctl info 查看。")

# 0. 服务器地址、密钥（导入时询问，见文末 WFWorkflowImportQuestions；ActionIndex 指向这两个「文本」动作）
IDX_SERVER = len(actions)
u_srv = act("gettext", WFTextActionText=SERVER_DEFAULT)
act("setvariable", WFInput=attach(out(u_srv, "文本")), WFVariableName="server")
IDX_TOKEN = len(actions)
u_tok = act("gettext", WFTextActionText=TOKEN_DEFAULT)
act("setvariable", WFInput=attach(out(u_tok, "文本")), WFVariableName="token")

API = text(OBJ + "/api", {0: var("server")})
headers = dict_items([("X-Token", text(OBJ, {0: var("token")}))])

# 1. 提交任务
u_post = act("downloadurl", WFURL=API, WFHTTPMethod="POST", WFHTTPBodyType="Form", ShowHeaders=True,
             WFHTTPHeaders=headers,
             WFFormValues=dict_items([("text", text(OBJ, {0: {"Type": "ExtensionInput"}}))]))
u_d1 = act("detect.dictionary", WFInput=attach(out(u_post, "URL的内容")))
u_id = act("getvalueforkey", WFInput=attach(out(u_d1, "词典")), WFGetDictionaryValueType="Value", WFDictionaryKey="id")
act("setvariable", WFInput=attach(out(u_id, "词典值")), WFVariableName="jobId")

g = uid()
ctrl("conditional", 0, g, WFCondition=101, WFInput={"Type": "Variable", "Variable": attach(var("jobId"))})
u_msg = act("getvalueforkey", WFInput=attach(out(u_d1, "词典")), WFGetDictionaryValueType="Value", WFDictionaryKey="msg")
act("alert", WFAlertActionTitle="提交失败", WFAlertActionCancelButtonShown=False,
    WFAlertActionMessage=text(OBJ, {0: out(u_msg, "词典值")}))
act("exit")
ctrl("conditional", 2, g)

u_pmsg = act("getvalueforkey", WFInput=attach(out(u_d1, "词典")), WFGetDictionaryValueType="Value", WFDictionaryKey="msg")
act("notification", WFNotificationActionBody=text(OBJ, {0: out(u_pmsg, "词典值")}))

# 2. 轮询（最多约 5 分钟）
g_rep = uid()
ctrl("repeat.count", 0, g_rep, WFRepeatCount=150)
act("delay", WFDelayTime=2)
u_get = act("downloadurl", WFURL=text(OBJ + "/api?id=" + OBJ, {0: var("server"), 9: var("jobId")}),
            WFHTTPMethod="GET", ShowHeaders=True, WFHTTPHeaders=headers)
u_d2 = act("detect.dictionary", WFInput=attach(out(u_get, "URL的内容")))
u_st = act("getvalueforkey", WFInput=attach(out(u_d2, "词典")), WFGetDictionaryValueType="Value", WFDictionaryKey="status")
act("setvariable", WFInput=attach(out(u_st, "词典值")), WFVariableName="status")
# 服务器只在 25/50/75%、换阶段、下完时给 notify，取走即清空，所以不会刷屏
u_nt = act("getvalueforkey", WFInput=attach(out(u_d2, "词典")), WFGetDictionaryValueType="Value", WFDictionaryKey="notify")
act("setvariable", WFInput=attach(out(u_nt, "词典值")), WFVariableName="notify")
g_nt = uid()
ctrl("conditional", 0, g_nt, WFCondition=100, WFInput={"Type": "Variable", "Variable": attach(var("notify"))})
act("notification", WFNotificationActionBody=text(OBJ, {0: var("notify")}))
ctrl("conditional", 2, g_nt)

g_done = uid()
if_contains(var("status"), "done", g_done)
# 不用「重复每一项」：iOS 导入后「重复项目」变量会失效（红色），「获取 URL 内容」传多个链接又只下第一个。
# 改为计数器 n 循环，按键名 f1、f2… 逐个取文件地址（服务端在结果里提供这些键）。
u_zero = act("number", WFNumberActionNumber=0)
act("setvariable", WFInput=attach(out(u_zero, "数字")), WFVariableName="n")
g_files = uid()
ctrl("repeat.count", 0, g_files, WFRepeatCount=20)
u_inc = act("math", WFInput=attach(var("n")), WFMathOperation="+", WFMathOperand=1)
act("setvariable", WFInput=attach(out(u_inc, "计算结果")), WFVariableName="n")
u_f = act("getvalueforkey", WFInput=attach(out(u_d2, "词典")), WFGetDictionaryValueType="Value",
          WFDictionaryKey=text("f" + OBJ, {1: var("n")}))
act("setvariable", WFInput=attach(out(u_f, "词典值")), WFVariableName="fileUrl")
g_has = uid()
ctrl("conditional", 0, g_has, WFCondition=100, WFInput={"Type": "Variable", "Variable": attach(var("fileUrl"))})
u_dl = act("downloadurl", WFURL=text(OBJ, {0: var("fileUrl")}), WFHTTPMethod="GET")
act("savetocameraroll", WFInput=attach(out(u_dl, "URL的内容")))
ctrl("conditional", 2, g_has)
ctrl("repeat.count", 2, g_files)
act("notification", WFNotificationActionBody="✅ 下载完成，已保存到相册")
act("exit")
ctrl("conditional", 2, g_done)

g_err = uid()
if_contains(var("status"), "error", g_err)
u_emsg = act("getvalueforkey", WFInput=attach(out(u_d2, "词典")), WFGetDictionaryValueType="Value", WFDictionaryKey="msg")
act("alert", WFAlertActionTitle="下载失败", WFAlertActionCancelButtonShown=False,
    WFAlertActionMessage=text(OBJ, {0: out(u_emsg, "词典值")}))
act("exit")
ctrl("conditional", 2, g_err)
ctrl("repeat.count", 2, g_rep)

act("alert", WFAlertActionTitle="等待超时", WFAlertActionCancelButtonShown=False,
    WFAlertActionMessage=text("服务器还在下载（视频可能太长），稍后再试一次"))

shortcut = {
    "WFWorkflowActions": actions,
    "WFWorkflowClientVersion": "4046.0.2.1.102",
    "WFWorkflowMinimumClientVersion": 900,
    "WFWorkflowMinimumClientVersionString": "900",
    "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59864},
    "WFWorkflowInputContentItemClasses": ["WFArticleContentItem", "WFRichTextContentItem",
                                          "WFSafariWebPageContentItem", "WFStringContentItem", "WFURLContentItem"],
    "WFWorkflowNoInputBehavior": {"Name": "WFWorkflowNoInputBehaviorGetClipboard", "Parameters": {}},
    "WFWorkflowHasShortcutInputVariables": True,
    "WFWorkflowOutputContentItemClasses": [],
    "WFWorkflowHasOutputFallback": False,
    "WFWorkflowImportQuestions": [
        {"ActionIndex": IDX_SERVER, "Category": "Parameter", "ParameterKey": "WFTextActionText",
         "Text": "服务器地址（服务器上执行 vdlctl info 查看，形如 https://域名/随机路径，末尾不要加 /）",
         "DefaultValue": SERVER_DEFAULT},
        {"ActionIndex": IDX_TOKEN, "Category": "Parameter", "ParameterKey": "WFTextActionText",
         "Text": "密钥（vdlctl info 里的「密钥」）", "DefaultValue": TOKEN_DEFAULT},
    ],
    "WFWorkflowTypes": ["ActionExtension"],
    "WFQuickActionSurfaces": [],
}
with open(OUT, "wb") as f:
    plistlib.dump(shortcut, f, fmt=plistlib.FMT_BINARY)
print("actions:", len(actions))

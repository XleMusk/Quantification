"""Committee lenses inspired by public investing and trading principles.

The system does not impersonate real investors. Each lens summarizes a
well-known school of thought so the user can pressure-test an idea from
multiple angles before any real capital is committed.
"""

COMMITTEE_PROFILES = [
    {
        "id": "business_owner",
        "name": "好生意与安全边际席",
        "inspired_by": "价值投资、企业所有者思维",
        "mission": "判断这家公司是否值得长期拥有，以及价格是否留有安全边际。",
        "questions": [
            "它靠什么赚钱，利润质量是否稳定？",
            "护城河来自品牌、成本、网络、牌照，还是只是周期运气？",
            "如果股价明天停牌一年，你还愿意持有吗？",
            "估值已经透支了多少乐观假设？",
        ],
        "required_paths": [
            ("business", "商业模式"),
            ("fundamentals.roe", "ROE/盈利质量"),
            ("fundamentals.cashflow", "现金流"),
            ("valuation.comparison", "估值对比"),
            ("risks", "风险清单"),
        ],
    },
    {
        "id": "growth_detective",
        "name": "成长故事与一线调研席",
        "inspired_by": "彼得林奇式成长股研究",
        "mission": "检查增长故事是否真实、可验证、能被普通投资者理解。",
        "questions": [
            "增长来自销量、价格、份额、品类扩张，还是一次性因素？",
            "你是否能用三句话讲清楚这家公司未来为什么会更好？",
            "身边、渠道、客户、产品有没有可观察证据？",
            "业绩兑现的时间表是什么？",
        ],
        "required_paths": [
            ("buy_reason", "买入理由"),
            ("catalysts", "催化剂"),
            ("fundamentals.revenue_growth", "收入增速"),
            ("fundamentals.profit_growth", "利润增速"),
            ("field_research", "一线观察/调研"),
        ],
    },
    {
        "id": "trend_risk",
        "name": "趋势与止损纪律席",
        "inspired_by": "强势股、趋势跟随、风险优先交易体系",
        "mission": "确认价格行为是否支持交易，并要求先定义错误边界。",
        "questions": [
            "股价是否处于清晰上升趋势，还是只是在反弹？",
            "成交量是否支持突破或趋势延续？",
            "如果买入后马上错了，跌到哪里必须退出？",
            "潜在收益至少是否值得承担这次亏损？",
        ],
        "required_paths": [
            ("technical.trend", "趋势状态"),
            ("technical.relative_strength", "相对强弱"),
            ("technical.volume", "成交量"),
            ("planned_stop_loss_pct", "预设止损"),
            ("sell_rules.wrong", "错了卖规则"),
        ],
    },
    {
        "id": "macro_asymmetry",
        "name": "宏观赔率与反身性席",
        "inspired_by": "宏观交易、赔率、仓位弹性",
        "mission": "检查市场共识、政策周期、行业景气和赔率是否匹配。",
        "questions": [
            "这笔交易押注的宏观或行业变量是什么？",
            "市场当前共识是什么，你与共识的差异在哪里？",
            "如果你对了，收益空间是否明显大于亏损空间？",
            "政策、流动性、汇率、利率会不会改变故事？",
        ],
        "required_paths": [
            ("market_context", "市场/行业背景"),
            ("consensus_view", "市场共识"),
            ("variant_view", "你的差异化判断"),
            ("catalysts", "催化剂"),
            ("risks", "风险清单"),
        ],
    },
    {
        "id": "index_skeptic",
        "name": "指数化怀疑席",
        "inspired_by": "低成本、分散、反过度交易原则",
        "mission": "追问你为什么不买指数，以及主动选择是否真的有优势。",
        "questions": [
            "这笔主动交易的优势来自信息、分析、行为纪律，还是错觉？",
            "同样资金买沪深300/中证500/红利指数，胜算是否更稳？",
            "交易成本、时间成本和心理成本是否值得？",
            "如果没有明确优势，是否应该降低仓位或只观察？",
        ],
        "required_paths": [
            ("benchmark", "比较基准"),
            ("edge", "主动优势"),
            ("time_horizon", "持有周期"),
            ("planned_capital_pct", "计划仓位"),
        ],
    },
    {
        "id": "a_share_execution",
        "name": "A股执行与风控席",
        "inspired_by": "实盘交易风控、A股交易规则",
        "mission": "确认交易在A股规则、账户权限、流动性和风控上可执行。",
        "questions": [
            "是否ST、停牌、涨跌停、权限受限或流动性过低？",
            "买入是否按100股整数手，卖出是否受T+1和可卖持仓限制？",
            "单票、总仓位、单日亏损是否在限制内？",
            "如果行情、账户、委托回报异常，是否停止交易？",
        ],
        "required_paths": [
            ("a_share_constraints.st", "ST状态"),
            ("a_share_constraints.halted", "停牌状态"),
            ("a_share_constraints.limit_status", "涨跌停状态"),
            ("a_share_constraints.liquidity", "流动性"),
            ("risk_limits.max_single_position_pct", "单票上限"),
        ],
    },
]


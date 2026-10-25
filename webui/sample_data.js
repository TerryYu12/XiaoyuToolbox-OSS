// 本地兜底样本数据（由 前端样本数据.json 同步生成）
window.FALLBACK_DATA = {
  "generated_at": "2026-09-17 22:16:49",
  "note": "v4：ZCode 用 model_usage 权威表；Antigravity 改为官方配额（kind=quota，非 token）",
  "summary_all": {
    "generated_at": "2026-09-17 22:16:49",
    "window_days": null,
    "hermes": {
      "agents": 5,
      "sessions": 412,
      "input": 378512724,
      "output": 12637521,
      "cache_read": 1692265076,
      "cache_write": 5836275,
      "reasoning": 5955135,
      "cost_usd": 0.3854,
      "cost_anomaly_agents": [
        "terra"
      ],
      "cost_anomaly_usd": 18300529.57
    },
    "ccs": {
      "requests": 13346,
      "requests_proxy": 12880,
      "requests_session": 466,
      "success_requests": 11769,
      "input": 1330054325,
      "output": 8859571,
      "cache_read": 1496200576,
      "cache_creation": 2589596,
      "cache_hit_rate": 0.5289,
      "cost_usd": 174.8438,
      "avg_latency_ms": 13700,
      "avg_first_token_ms": 11391,
      "by_app_type": [
        {
          "app_type": "claude",
          "requests": 11,
          "input": 27883,
          "output": 430,
          "cache_read": 1024,
          "cache_creation": 0,
          "cache_hit_rate": 0.0354
        },
        {
          "app_type": "claude-desktop",
          "requests": 3834,
          "input": 37168314,
          "output": 2683541,
          "cache_read": 507870016,
          "cache_creation": 1430639,
          "cache_hit_rate": 0.9294
        },
        {
          "app_type": "codex",
          "requests": 9035,
          "input": 1219450394,
          "output": 5995536,
          "cache_read": 966975316,
          "cache_creation": 1127088,
          "cache_hit_rate": 0.442
        }
      ]
    }
  },
  "summary_7d": {
    "generated_at": "2026-09-17 22:16:49",
    "window_days": 7,
    "hermes": {
      "agents": 5,
      "sessions": 66,
      "input": 43019635,
      "output": 4845864,
      "cache_read": 868781903,
      "cache_write": 0,
      "reasoning": 2869697,
      "cost_usd": 0,
      "cost_anomaly_agents": [
        "terra"
      ],
      "cost_anomaly_usd": 18300529.57
    },
    "ccs": {
      "requests": 3832,
      "requests_proxy": 3366,
      "requests_session": 466,
      "success_requests": 3305,
      "input": 108142367,
      "output": 2850274,
      "cache_read": 526462923,
      "cache_creation": 987350,
      "cache_hit_rate": 0.8283,
      "cost_usd": 174.8438,
      "avg_latency_ms": 10671,
      "avg_first_token_ms": 5934,
      "by_app_type": [
        {
          "app_type": "claude",
          "requests": 3,
          "input": 7,
          "output": 5,
          "cache_read": 0,
          "cache_creation": 0,
          "cache_hit_rate": 0.0
        },
        {
          "app_type": "claude-desktop",
          "requests": 3304,
          "input": 34624933,
          "output": 2630446,
          "cache_read": 505044274,
          "cache_creation": 955481,
          "cache_hit_rate": 0.9342
        },
        {
          "app_type": "codex",
          "requests": 59,
          "input": 109693,
          "output": 39759,
          "cache_read": 64429,
          "cache_creation": 0,
          "cache_hit_rate": 0.37
        }
      ]
    }
  },
  "summary_1d": {
    "generated_at": "2026-09-17 22:16:49",
    "window_days": 1,
    "hermes": {
      "agents": 5,
      "sessions": 28,
      "input": 28496049,
      "output": 2480166,
      "cache_read": 421672030,
      "cache_write": 0,
      "reasoning": 1567928,
      "cost_usd": 0,
      "cost_anomaly_agents": [
        "terra"
      ],
      "cost_anomaly_usd": 18186781.57
    },
    "ccs": {
      "requests": 1125,
      "requests_proxy": 659,
      "requests_session": 466,
      "success_requests": 656,
      "input": 75075028,
      "output": 807756,
      "cache_read": 177940716,
      "cache_creation": 31869,
      "cache_hit_rate": 0.7032,
      "cost_usd": 174.8438,
      "avg_latency_ms": 9626,
      "avg_first_token_ms": 5145,
      "by_app_type": [
        {
          "app_type": "claude",
          "requests": 1,
          "input": 0,
          "output": 0,
          "cache_read": 0,
          "cache_creation": 0,
          "cache_hit_rate": 0.0
        },
        {
          "app_type": "claude-desktop",
          "requests": 658,
          "input": 1667294,
          "output": 627692,
          "cache_read": 156586496,
          "cache_creation": 0,
          "cache_hit_rate": 0.9895
        }
      ]
    }
  },
  "agents": [
    {
      "name": "default",
      "sessions": 384,
      "input": 364711502,
      "output": 11955350,
      "cache_read": 1608233365,
      "cache_write": 5836275,
      "reasoning": 5590060,
      "cost_usd": 0.385392,
      "cost_anomaly": false,
      "actual_cost_usd": 0,
      "messages": 27445,
      "tool_calls": 13842,
      "cache_hit_rate": 0.8127,
      "models": [
        {
          "model": "DeepSeek-V4-Flash-0731",
          "sessions": 42,
          "input": 100079336,
          "output": 1826324,
          "cache_read": 175996747
        },
        {
          "model": "gpt-5.6-terra",
          "sessions": 22,
          "input": 63772959,
          "output": 801162,
          "cache_read": 175991207
        },
        {
          "model": "deepseek-v4-flash",
          "sessions": 81,
          "input": 34763398,
          "output": 1807591,
          "cache_read": 82515705
        },
        {
          "model": "deepseek-v4-flash-vision-exp",
          "sessions": 8,
          "input": 34248163,
          "output": 416126,
          "cache_read": 30779392
        },
        {
          "model": "gemini-3.7-flash",
          "sessions": 5,
          "input": 22968399,
          "output": 279461,
          "cache_read": 4915360
        },
        {
          "model": "default",
          "sessions": 2,
          "input": 22184913,
          "output": 242006,
          "cache_read": 44375676
        },
        {
          "model": "claude-fable-5",
          "sessions": 1,
          "input": 17684219,
          "output": 99796,
          "cache_read": 2238157
        },
        {
          "model": "deepseek/deepseek-v4.1-flash",
          "sessions": 18,
          "input": 17534122,
          "output": 3330997,
          "cache_read": 648948451
        }
      ]
    },
    {
      "name": "luna",
      "sessions": 2,
      "input": 626682,
      "output": 94955,
      "cache_read": 9813055,
      "cache_write": 0,
      "reasoning": 64801,
      "cost_usd": 0,
      "cost_anomaly": false,
      "actual_cost_usd": 0,
      "messages": 243,
      "tool_calls": 112,
      "cache_hit_rate": 0.94,
      "models": [
        {
          "model": "gpt-5.6-luna",
          "sessions": 1,
          "input": 626682,
          "output": 94955,
          "cache_read": 9813055
        },
        {
          "model": "deepseek-v4-flash-free",
          "sessions": 1,
          "input": 0,
          "output": 0,
          "cache_read": 0
        }
      ]
    },
    {
      "name": "qa",
      "sessions": 3,
      "input": 796687,
      "output": 193625,
      "cache_read": 21787648,
      "cache_write": 0,
      "reasoning": 120460,
      "cost_usd": 0,
      "cost_anomaly": false,
      "actual_cost_usd": 0,
      "messages": 452,
      "tool_calls": 223,
      "cache_hit_rate": 0.9647,
      "models": [
        {
          "model": "deepseek/deepseek-v4-flash",
          "sessions": 2,
          "input": 744963,
          "output": 163215,
          "cache_read": 20140288
        },
        {
          "model": "deepseek/deepseek-v4.1-flash",
          "sessions": 1,
          "input": 51724,
          "output": 30410,
          "cache_read": 1647360
        }
      ]
    },
    {
      "name": "tdxd",
      "sessions": 18,
      "input": 11822582,
      "output": 225270,
      "cache_read": 42546400,
      "cache_write": 0,
      "reasoning": 44526,
      "cost_usd": 0,
      "cost_anomaly": false,
      "actual_cost_usd": 0,
      "messages": 872,
      "tool_calls": 473,
      "cache_hit_rate": 0.7825,
      "models": [
        {
          "model": "gpt-5.6-sol",
          "sessions": 9,
          "input": 10645722,
          "output": 157350,
          "cache_read": 35591200
        },
        {
          "model": "gpt-5.6-luna",
          "sessions": 6,
          "input": 665072,
          "output": 32696,
          "cache_read": 3431936
        },
        {
          "model": "deepseek/deepseek-v4.1-flash",
          "sessions": 2,
          "input": 481432,
          "output": 35122,
          "cache_read": 3517184
        },
        {
          "model": "glm-5.3",
          "sessions": 1,
          "input": 30356,
          "output": 102,
          "cache_read": 6080
        }
      ]
    },
    {
      "name": "terra",
      "sessions": 5,
      "input": 555271,
      "output": 168321,
      "cache_read": 9884608,
      "cache_write": 0,
      "reasoning": 135288,
      "cost_usd": 18300529.569508,
      "cost_anomaly": true,
      "actual_cost_usd": 0,
      "messages": 302,
      "tool_calls": 130,
      "cache_hit_rate": 0.9468,
      "models": [
        {
          "model": "gpt-5.6-terra",
          "sessions": 1,
          "input": 495495,
          "output": 132869,
          "cache_read": 9391744
        },
        {
          "model": "glm-5.3",
          "sessions": 3,
          "input": 59776,
          "output": 35452,
          "cache_read": 492864
        },
        {
          "model": "deepseek/deepseek-v4-flash",
          "sessions": 1,
          "input": 0,
          "output": 0,
          "cache_read": 0
        }
      ]
    }
  ],
  "models": [
    {
      "model": "gpt-5.6-sol",
      "requests": 2852,
      "input": 529036636,
      "output": 1572277,
      "cache_read": 469313845,
      "cache_creation": 0,
      "total_tokens": 999922758,
      "cache_hit_rate": 0.4701
    },
    {
      "model": "gpt-5.6-terra",
      "requests": 1921,
      "input": 384253764,
      "output": 1166829,
      "cache_read": 317239062,
      "cache_creation": 0,
      "total_tokens": 702659655,
      "cache_hit_rate": 0.4522
    },
    {
      "model": "deepseek/deepseek-v4.1-flash",
      "requests": 2551,
      "input": 33325401,
      "output": 2269225,
      "cache_read": 443008000,
      "cache_creation": 0,
      "total_tokens": 478602626,
      "cache_hit_rate": 0.93
    },
    {
      "model": "gpt-5.6-luna",
      "requests": 2432,
      "input": 116917935,
      "output": 891283,
      "cache_read": 89512357,
      "cache_creation": 1127088,
      "total_tokens": 208448663,
      "cache_hit_rate": 0.4313
    },
    {
      "model": "glm-5.3-flash",
      "requests": 545,
      "input": 54331720,
      "output": 338467,
      "cache_read": 48314560,
      "cache_creation": 0,
      "total_tokens": 102984747,
      "cache_hit_rate": 0.4707
    },
    {
      "model": "deepseek-v4-flash-0731",
      "requests": 471,
      "input": 58272232,
      "output": 1417386,
      "cache_read": 24125544,
      "cache_creation": 0,
      "total_tokens": 83815162,
      "cache_hit_rate": 0.2928
    },
    {
      "model": "qwen3.8-flash",
      "requests": 370,
      "input": 1057904,
      "output": 311611,
      "cache_read": 61204224,
      "cache_creation": 0,
      "total_tokens": 62573739,
      "cache_hit_rate": 0.983
    },
    {
      "model": "deepseek-v4-flash",
      "requests": 293,
      "input": 35957224,
      "output": 247012,
      "cache_read": 12799488,
      "cache_creation": 0,
      "total_tokens": 49003724,
      "cache_hit_rate": 0.2625
    },
    {
      "model": "DeepSeek-V4-Flash-0731",
      "requests": 366,
      "input": 16435567,
      "output": 138432,
      "cache_read": 5423872,
      "cache_creation": 0,
      "total_tokens": 21997871,
      "cache_hit_rate": 0.2481
    },
    {
      "model": "deepseek-v4-flash-vision-exp",
      "requests": 40,
      "input": 11618764,
      "output": 38775,
      "cache_read": 0,
      "cache_creation": 0,
      "total_tokens": 11657539,
      "cache_hit_rate": 0.0
    },
    {
      "model": "deepseek-v4-flash-fast",
      "requests": 163,
      "input": 10659984,
      "output": 123417,
      "cache_read": 151142,
      "cache_creation": 0,
      "total_tokens": 10934543,
      "cache_hit_rate": 0.014
    },
    {
      "model": "claude-opus-5",
      "requests": 409,
      "input": 357605,
      "output": 57066,
      "cache_read": 2795451,
      "cache_creation": 1022911,
      "total_tokens": 4233033,
      "cache_hit_rate": 0.6694
    },
    {
      "model": "deepseek-v4-flash-202605",
      "requests": 27,
      "input": 3069146,
      "output": 29855,
      "cache_read": 0,
      "cache_creation": 0,
      "total_tokens": 3099001,
      "cache_hit_rate": 0.0
    },
    {
      "model": "unknown",
      "requests": 2,
      "input": 502648,
      "output": 3311,
      "cache_read": 500480,
      "cache_creation": 0,
      "total_tokens": 1006439,
      "cache_hit_rate": 0.4989
    },
    {
      "model": "gpt-5.6-terra-lowcache",
      "requests": 5,
      "input": 368992,
      "output": 4036,
      "cache_read": 73216,
      "cache_creation": 0,
      "total_tokens": 446244,
      "cache_hit_rate": 0.1656
    },
    {
      "model": "claude-haiku-4-5-20251001",
      "requests": 162,
      "input": 57605,
      "output": 20324,
      "cache_read": 244480,
      "cache_creation": 383430,
      "total_tokens": 705839,
      "cache_hit_rate": 0.3566
    },
    {
      "model": "glm-5.3-free",
      "requests": 59,
      "input": 109693,
      "output": 39759,
      "cache_read": 64429,
      "cache_creation": 0,
      "total_tokens": 213881,
      "cache_hit_rate": 0.37
    },
    {
      "model": "claude-fable-5",
      "requests": 41,
      "input": 99013,
      "output": 4902,
      "cache_read": 67678,
      "cache_creation": 1707,
      "total_tokens": 173300,
      "cache_hit_rate": 0.4019
    },
    {
      "model": "gpt-5.5",
      "requests": 8,
      "input": 54884,
      "output": 445,
      "cache_read": 0,
      "cache_creation": 0,
      "total_tokens": 55329,
      "cache_hit_rate": 0.0
    },
    {
      "model": "gpt-6-astra",
      "requests": 3,
      "input": 46101,
      "output": 488,
      "cache_read": 7494,
      "cache_creation": 0,
      "total_tokens": 54083,
      "cache_hit_rate": 0.1398
    }
  ],
  "daily": [
    {
      "date": "2026-08-25",
      "requests": 330,
      "success": 330,
      "input": 33709274,
      "output": 162812,
      "cache_read": 31169664,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-08-26",
      "requests": 794,
      "success": 794,
      "input": 84616862,
      "output": 243442,
      "cache_read": 73259420,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-08-27",
      "requests": 658,
      "success": 642,
      "input": 65728559,
      "output": 389468,
      "cache_read": 58030528,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-08-28",
      "requests": 1824,
      "success": 1660,
      "input": 249912324,
      "output": 1138265,
      "cache_read": 187474304,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-08-29",
      "requests": 206,
      "success": 203,
      "input": 40473735,
      "output": 136327,
      "cache_read": 21895220,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-08-30",
      "requests": 1394,
      "success": 1262,
      "input": 259800206,
      "output": 837162,
      "cache_read": 209881043,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-08-31",
      "requests": 2579,
      "success": 2211,
      "input": 314959559,
      "output": 2477412,
      "cache_read": 249451666,
      "cache_creation": 1127088,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-01",
      "requests": 950,
      "success": 923,
      "input": 156389031,
      "output": 473931,
      "cache_read": 135143169,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-02",
      "requests": 312,
      "success": 153,
      "input": 1998931,
      "output": 42458,
      "cache_read": 2662582,
      "cache_creation": 399600,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-03",
      "requests": 274,
      "success": 273,
      "input": 14249183,
      "output": 106485,
      "cache_read": 604240,
      "cache_creation": 56576,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-06",
      "requests": 189,
      "success": 9,
      "input": 68660,
      "output": 1191,
      "cache_read": 7494,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-07",
      "requests": 4,
      "success": 4,
      "input": 5634,
      "output": 344,
      "cache_read": 158323,
      "cache_creation": 18982,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-12",
      "requests": 662,
      "success": 646,
      "input": 23598856,
      "output": 346472,
      "cache_read": 4722527,
      "cache_creation": 955481,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-13",
      "requests": 126,
      "success": 126,
      "input": 2034148,
      "output": 130772,
      "cache_read": 13434240,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-14",
      "requests": 995,
      "success": 982,
      "input": 5322102,
      "output": 848213,
      "cache_read": 162864256,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-15",
      "requests": 714,
      "success": 687,
      "input": 1750056,
      "output": 559978,
      "cache_read": 128462848,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-16",
      "requests": 318,
      "success": 316,
      "input": 488040,
      "output": 232107,
      "cache_read": 54514688,
      "cache_creation": 0,
      "cost_usd": 0.0
    },
    {
      "date": "2026-09-17",
      "requests": 551,
      "success": 548,
      "input": 1541431,
      "output": 552668,
      "cache_read": 141110144,
      "cache_creation": 0,
      "cost_usd": 0.0
    }
  ],
  "tools": [
    {
      "name": "Hermes·default",
      "group": "Hermes",
      "available": true,
      "source": "hermes:sessions",
      "requests": 389,
      "input": 366310312,
      "output": 11958886,
      "cache_read": 1636465998,
      "cache_write": 5836275,
      "total_tokens": 2020571471,
      "cache_hit_rate": 0.8147,
      "cost_usd": 0.385392,
      "cost_anomaly": false
    },
    {
      "name": "Hermes·luna",
      "group": "Hermes",
      "available": true,
      "source": "hermes:sessions",
      "requests": 2,
      "input": 626682,
      "output": 94955,
      "cache_read": 9813055,
      "cache_write": 0,
      "total_tokens": 10534692,
      "cache_hit_rate": 0.94,
      "cost_usd": 0,
      "cost_anomaly": false
    },
    {
      "name": "Hermes·qa",
      "group": "Hermes",
      "available": true,
      "source": "hermes:sessions",
      "requests": 3,
      "input": 796687,
      "output": 193625,
      "cache_read": 21787648,
      "cache_write": 0,
      "total_tokens": 22777960,
      "cache_hit_rate": 0.9647,
      "cost_usd": 0,
      "cost_anomaly": false
    },
    {
      "name": "Hermes·tdxd",
      "group": "Hermes",
      "available": true,
      "source": "hermes:sessions",
      "requests": 18,
      "input": 11822582,
      "output": 225270,
      "cache_read": 42546400,
      "cache_write": 0,
      "total_tokens": 54594252,
      "cache_hit_rate": 0.7825,
      "cost_usd": 0,
      "cost_anomaly": false
    },
    {
      "name": "Hermes·terra",
      "group": "Hermes",
      "available": true,
      "source": "hermes:sessions",
      "requests": 5,
      "input": 555271,
      "output": 168321,
      "cache_read": 9884608,
      "cache_write": 0,
      "total_tokens": 10608200,
      "cache_hit_rate": 0.9468,
      "cost_usd": 18300529.569508,
      "cost_anomaly": true
    },
    {
      "name": "Codex",
      "app_type": "codex",
      "available": true,
      "source": "ccs:proxy+session",
      "requests": 9037,
      "requests_proxy": 9035,
      "requests_session": 2,
      "input": 1219460780,
      "output": 5996287,
      "cache_read": 966975316,
      "cache_creation": 1127088,
      "total_tokens": 2193559471,
      "cache_hit_rate": 0.442,
      "models_count": 19,
      "cost_usd": 0.0,
      "group": "CCS"
    },
    {
      "name": "Claude Desktop",
      "app_type": "claude-desktop",
      "available": true,
      "source": "ccs:proxy+session",
      "requests": 3846,
      "requests_proxy": 3846,
      "requests_session": 0,
      "input": 37626541,
      "output": 2693068,
      "cache_read": 513518016,
      "cache_creation": 1430639,
      "total_tokens": 555268264,
      "cache_hit_rate": 0.9293,
      "models_count": 21,
      "cost_usd": 0.0,
      "group": "CCS"
    },
    {
      "name": "Claude Code (CC)",
      "app_type": "claude",
      "available": true,
      "source": "ccs:proxy+session",
      "requests": 475,
      "requests_proxy": 11,
      "requests_session": 464,
      "input": 73425231,
      "output": 179743,
      "cache_read": 21355244,
      "cache_creation": 31869,
      "total_tokens": 94992087,
      "cache_hit_rate": 0.2252,
      "models_count": 8,
      "cost_usd": 174.8438,
      "group": "CCS"
    },
    {
      "name": "ZCode",
      "available": true,
      "source": "zcode:model_usage",
      "sessions": 44,
      "requests": 2963,
      "input": 619269323,
      "output": 2288461,
      "cache_read": 601541120,
      "cache_write": 0,
      "total_tokens": 621557784,
      "provider_total_tokens": 621557784,
      "cache_hit_rate": 0.9714,
      "cost_usd": null,
      "avg_duration_ms": 29261,
      "avg_ttft_ms": 9495,
      "status_counts": {
        "cancelled": 23,
        "completed": 2902,
        "error": 38
      },
      "models": [
        {
          "model": "GLM-5.3-Flash",
          "requests": 2963,
          "input": 619269323,
          "output": 2288461,
          "cache_read": 601541120,
          "total": 621557784,
          "cache_hit_rate": 0.9714
        }
      ],
      "agents": [
        {
          "agent": "zcode-agent",
          "requests": 2706,
          "total_tokens": 606213249
        },
        {
          "agent": "zcode-general-purpose",
          "requests": 241,
          "total_tokens": 15117094
        },
        {
          "agent": "zcode-judge",
          "requests": 16,
          "total_tokens": 227441
        }
      ],
      "note": "input 含 cache_read（OpenAI 口径）；命中率=cache_read/input",
      "group": "ZCode"
    },
    {
      "name": "Antigravity",
      "group": "Antigravity",
      "sessions": 46,
      "steps": 27077,
      "available": true,
      "kind": "tokens+quota",
      "source": "antigravity:quota_rpc",
      "note": "官方配额（非 token）—— Antigravity 本地不记录 token 用量",
      "groups": [
        {
          "name": "Gemini Models",
          "models": "Models within this group: Gemini Flash, Gemini Pro",
          "buckets": [
            {
              "id": "gemini-weekly",
              "window": "weekly",
              "remaining_pct": 60.7,
              "reset_time": "2026-09-23T05:25:34Z",
              "desc": "You have used some of your weekly limit, it will fully refresh in 5 days, 14 hours."
            },
            {
              "id": "gemini-5h",
              "window": "5h",
              "remaining_pct": 56.4,
              "reset_time": "2026-09-17T16:51:31Z",
              "desc": "You have used some of your 5-hour limit, it will fully refresh in 1 hour, 57 minutes."
            }
          ]
        },
        {
          "name": "Claude and GPT models",
          "models": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
          "buckets": [
            {
              "id": "3p-weekly",
              "window": "weekly",
              "remaining_pct": 86.2,
              "reset_time": "2026-09-23T08:10:28Z",
              "desc": "You have used some of your weekly limit, it will fully refresh in 5 days, 17 hours."
            },
            {
              "id": "3p-5h",
              "window": "5h",
              "remaining_pct": 100,
              "reset_time": "2026-09-17T19:54:01Z",
              "desc": ""
            }
          ]
        }
      ],
      "quota_description": "Within each group, models share a weekly limit and a 5-hour limit. Quota is consumed proportionally to the cost of the tokens. Thus, limits will last longer with shorter tasks or using more cost-effective models. The 5-h",
      "tokens": {
        "name": "Antigravity",
        "available": true,
        "source": "antigravity:ls_rpc(modelUsage)",
        "note": "本地 LS 的真实记账（非估算）；LS 只保留仍在跟踪的会话",
        "tracked_conversations": 1,
        "sessions": 1,
        "requests": 224,
        "input": 5378457,
        "output": 300953,
        "cache_read": 28448781,
        "total_tokens": 5679410,
        "cache_hit_rate": 0.841,
        "by_day": [
          {
            "date": "2026-09-17",
            "requests": 224,
            "input": 5378457,
            "output": 300953,
            "cache_read": 28448781
          }
        ],
        "models": [
          {
            "model": "MODEL_PLACEHOLDER_M318",
            "requests": 224
          }
        ]
      }
    }
  ],
  "tools_generated_at": "2026-09-17 22:55:28",
  "ai_news": [
    {
      "id": "news-20260918-01",
      "title": "Anthropic 推出 Claude 3.7 Sonnet 混合推理模型与 Claude Code 生产级 CLI",
      "summary": "Anthropic 正式发布业界首个集标准生成与扩展思维链于一体的混合推理模型 Claude 3.7 Sonnet。同时其研发的高效终端智能体工具 Claude Code 凭借极低延迟与深度上下文整合，成为全网开发者最受瞩目的 Agent 级命令行生产力工具。",
      "category": "dev",
      "category_name": "编程 Agent",
      "source": "Anthropic Official",
      "source_url": "https://www.anthropic.com/news",
      "published_at": "2026-09-18 10:30",
      "tags": ["Claude 3.7", "Claude Code", "Agent", "混合推理"],
      "hot": true,
      "views": "18.9k"
    },
    {
      "id": "news-20260918-02",
      "title": "DeepSeek 全面开放开源生态：R1 与 V4 系列模型高并发推理成本突破新低",
      "summary": "DeepSeek 团队持续推进高性价比大模型普惠，V4 Flash 与 V3 系列模型以极低的 Token 成本和优秀的编码能力在开发者生态中迅速扩张。国内外各类反代、聚合路由网关已实现秒级平替与海量并发分发。",
      "category": "models",
      "category_name": "模型发布",
      "source": "DeepSeek Hub",
      "source_url": "https://deepseek.com",
      "published_at": "2026-09-18 09:15",
      "tags": ["DeepSeek-V4", "开源", "低成本推理", "API"],
      "hot": true,
      "views": "24.5k"
    },
    {
      "id": "news-20260917-03",
      "title": "OpenAI 启动下一代 Codex 生态重构：推出统一计算机控制协议与全平台桌面集成",
      "summary": "OpenAI 宣布升级 Codex CLI 与桌面客户端生态架构，引入深度的原生沙盒与统一计算机使用（Unified Computer Use）能力。支持与第三方本地代理无缝兼容，大幅提升复杂代码库重构能力。",
      "category": "dev",
      "category_name": "编程 Agent",
      "source": "OpenAI Engineering",
      "source_url": "https://openai.com/blog",
      "published_at": "2026-09-17 18:00",
      "tags": ["Codex", "OpenAI", "Computer Use", "CLI"],
      "hot": false,
      "views": "15.2k"
    },
    {
      "id": "news-20260917-04",
      "title": "Google 发布 Gemini 2.0 系列旗舰模型：原生多模态实时流式交互全面落地",
      "summary": "Gemini 2.0 Flash 与 Flash-Lite 展现出惊人的端到端实时流式能力。通过 Live API 突破音频与视觉协同瓶颈，并在长上下文召回率（2M Window）保持 99.8% 的极高精确度。",
      "category": "models",
      "category_name": "模型发布",
      "source": "Google DeepMind",
      "source_url": "https://deepmind.google/technologies/gemini/",
      "published_at": "2026-09-17 14:20",
      "tags": ["Gemini 2.0", "多模态", "实时流式", "Long Context"],
      "hot": true,
      "views": "19.8k"
    }
  ]
};

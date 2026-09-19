"""OKX Agent 自动交易模块。

与行情模块(packages/marketdata vendors/okx.py)完全解耦:
- 行情走免鉴权公共接口,属于 marketdata 包;
- 本模块走签名私有接口(下单/撤单/订单状态/账户余额),只被本模块使用。

安全约定:API Key/Secret/Passphrase 只从环境变量或密钥存储表读取,
禁止硬编码、禁止明文打日志。
"""

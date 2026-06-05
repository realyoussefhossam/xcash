# Tron VaultSlot Deployment Order

源码与 EVM 共用：`xcash/evm/contracts/src/`（本工程 `foundry.toml` 的 `src` 已指向它）。

1. `cd xcash/tron/contracts && forge build`（编译共用的 EVM 源码到 `out/`）。
2. Deploy `XcashVaultSlotTemplate`.
3. Deploy `XcashVaultSlotFactory(template_address)`.
4. Export deployed addresses as:

```bash
export TRON_VAULT_SLOT_TEMPLATE_ADDRESS="T..."
export TRON_VAULT_SLOT_FACTORY_ADDRESS="T..."
```

5. Run the Nile verification scripts from `xcash/tron/nile_verification/`.

Do not use OpenZeppelin's on-chain deterministic-address prediction on Tron.
The application-side predictor uses the TVM `0x41` CREATE2 address preimage.

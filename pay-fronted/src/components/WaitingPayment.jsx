import { useI18n } from "@/hooks/useI18n"

/**
 * 等待状态条 - waiting 状态
 * 默认提示用户尽快付款；broadcasted 为 true 表示用户已通过钱包广播交易，
 * 文案切到「等待区块确认」，与支付卡片内「交易已提交」保持一致。
 */
function WaitingPayment({ broadcasted }) {
  const { t } = useI18n()

  return (
    <div className="flex items-center gap-3.5 rounded-xl border bg-card px-5 py-4 shadow-sm animate-in fade-in-0 slide-in-from-bottom-4 duration-500">
      {/* 雷达脉冲 */}
      <span className="relative flex size-2.5 shrink-0">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand opacity-60" />
        <span className="relative inline-flex size-2.5 rounded-full bg-brand" />
      </span>
      <div className="min-w-0">
        <p className="text-sm font-medium">
          {t(broadcasted ? "waiting.broadcastTitle" : "waiting.title")}
        </p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {t(broadcasted ? "waiting.broadcastDescription" : "waiting.description")}
        </p>
      </div>
    </div>
  )
}

export default WaitingPayment

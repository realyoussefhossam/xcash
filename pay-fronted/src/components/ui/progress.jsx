"use client"

import * as React from "react"
import { Progress as ProgressPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

function Progress({
  className,
  value,
  indicatorClassName,
  ...props
}) {
  const progress = Math.max(0, Math.min(100, Number(value) || 0))

  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      className={cn(
        "relative h-2 w-full overflow-hidden rounded-full bg-success-soft",
        className
      )}
      {...props}>
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className={cn("h-full w-full flex-1 bg-success transition-all", indicatorClassName)}
        style={{ transform: `translateX(-${100 - progress}%)` }} />
    </ProgressPrimitive.Root>
  );
}

export { Progress }

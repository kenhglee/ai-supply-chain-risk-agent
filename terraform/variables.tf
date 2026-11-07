variable "risk_agent_image_digest" {
  description = "Immutable ECR image digest deployed to the Risk Agent Lambda"
  type        = string
}

variable "risk_agent_reserved_concurrency" {
  description = "Reserved concurrency for the Risk Agent Lambda: -1 = unreserved (default, current behavior); 0 = hold the function stopped (rollout); a positive value reserves and caps concurrency"
  type        = number
  default     = -1

  validation {
    condition     = var.risk_agent_reserved_concurrency >= -1 && floor(var.risk_agent_reserved_concurrency) == var.risk_agent_reserved_concurrency
    error_message = "risk_agent_reserved_concurrency must be a whole number >= -1 (-1 unreserved, 0 stopped)."
  }
}

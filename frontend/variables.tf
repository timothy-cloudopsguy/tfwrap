variable "environment" {
  description = "Environment to deploy (matches properties.<env>.(json|yaml|yml))"
  type        = string
  default     = "dev"
}

variable "app_name" {
  description = "Optional override for app_name; defaults to value in properties file"
  type        = string
  default     = ""
} 

variable "core_name" {
  description = "Optional override for core_name; defaults to value in properties file"
  type        = string
  default     = ""
} 
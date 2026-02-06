# Azure infrastructure - managed by the infra/platform team
# Temporal orchestrates the init/plan/apply lifecycle
# App team never touches this - they deploy to what we create here

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.80"
    }
  }
}

provider "azurerm" {
  features {}
}

# -- variables (passed in from Temporal via -var flags) --

variable "project_name" {
  type        = string
  description = "Used for naming everything"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "region" {
  type    = string
  default = "uksouth"
}

variable "vm_size" {
  type    = string
  default = "Standard_B2s"
}

variable "vnet_address_space" {
  type    = string
  default = "10.0.0.0/16"
}

variable "subnet_prefix" {
  type    = string
  default = "10.0.1.0/24"
}

variable "admin_username" {
  type    = string
  default = "azureadmin"
}

# -- resource group --

resource "azurerm_resource_group" "main" {
  name     = "rg-${var.project_name}-${var.environment}"
  location = var.region

  tags = {
    project     = var.project_name
    environment = var.environment
    managed_by  = "temporal"
  }
}

# -- networking --

resource "azurerm_virtual_network" "main" {
  name                = "vnet-${var.project_name}-${var.environment}"
  address_space       = [var.vnet_address_space]
  location            = var.region
  resource_group_name = azurerm_resource_group.main.name

  tags = {
    project     = var.project_name
    environment = var.environment
  }
}

resource "azurerm_network_security_group" "main" {
  name                = "nsg-${var.project_name}-${var.environment}"
  location            = var.region
  resource_group_name = azurerm_resource_group.main.name

  # SSH - you'd lock this down to specific IPs in prod
  security_rule {
    name                       = "AllowSSH"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  # HTTP for the app
  security_rule {
    name                       = "AllowHTTP"
    priority                   = 200
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8080"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  tags = {
    project     = var.project_name
    environment = var.environment
  }
}

resource "azurerm_subnet" "main" {
  name                 = "subnet-${var.project_name}-default"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.subnet_prefix]
}

resource "azurerm_subnet_network_security_group_association" "main" {
  subnet_id                 = azurerm_subnet.main.id
  network_security_group_id = azurerm_network_security_group.main.id
}

resource "azurerm_public_ip" "main" {
  name                = "pip-${var.project_name}-${var.environment}"
  location            = var.region
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"

  tags = {
    project     = var.project_name
    environment = var.environment
  }
}

# -- compute --

resource "azurerm_network_interface" "main" {
  name                = "nic-${var.project_name}-${var.environment}"
  location            = var.region
  resource_group_name = azurerm_resource_group.main.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.main.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.main.id
  }

  tags = {
    project     = var.project_name
    environment = var.environment
  }
}

resource "azurerm_linux_virtual_machine" "main" {
  name                = "vm-${var.project_name}-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = var.region
  size                = var.vm_size
  admin_username      = var.admin_username

  network_interface_ids = [azurerm_network_interface.main.id]

  admin_ssh_key {
    username   = var.admin_username
    public_key = file("~/.ssh/id_rsa.pub")
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }

  tags = {
    project     = var.project_name
    environment = var.environment
    managed_by  = "temporal"
  }
}

# -- outputs (Temporal grabs these via `terraform output -json`) --

output "resource_group_name" { value = azurerm_resource_group.main.name }
output "vnet_name" { value = azurerm_virtual_network.main.name }
output "subnet_id" { value = azurerm_subnet.main.id }
output "nsg_name" { value = azurerm_network_security_group.main.name }
output "public_ip_address" { value = azurerm_public_ip.main.ip_address }
output "vm_name" { value = azurerm_linux_virtual_machine.main.name }
output "vm_id" { value = azurerm_linux_virtual_machine.main.id }
output "private_ip_address" { value = azurerm_network_interface.main.private_ip_address }
output "admin_username" { value = var.admin_username }

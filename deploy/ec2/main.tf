terraform {
  required_version = "= 1.13.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.73.0"
    }
  }
  backend "local" {}
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      Project   = var.lab_name
      ManagedBy = "OpenTofu"
      Purpose   = "CloudOps release lab"
    }
  }
}

data "aws_ami" "lab" {
  owners = [var.ami_owner_id]

  filter {
    name   = "image-id"
    values = [var.ami_id]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

data "aws_ec2_instance_type" "lab" {
  instance_type = var.instance_type
}

resource "aws_vpc" "lab" {
  cidr_block           = "10.42.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
}

resource "aws_subnet" "lab" {
  vpc_id                  = aws_vpc.lab.id
  cidr_block              = "10.42.0.0/26"
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = false
}

resource "aws_internet_gateway" "lab" {
  vpc_id = aws_vpc.lab.id
}

resource "aws_route_table" "lab" {
  vpc_id = aws_vpc.lab.id
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.lab.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.lab.id
}

resource "aws_route_table_association" "lab" {
  subnet_id      = aws_subnet.lab.id
  route_table_id = aws_route_table.lab.id
}

resource "aws_security_group" "host" {
  name_prefix = "${var.lab_name}-"
  vpc_id      = aws_vpc.lab.id
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.host.id
  cidr_ipv4         = var.operator_ipv4_cidr
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.host.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_instance" "lab" {
  ami                         = data.aws_ami.lab.id
  instance_type               = var.instance_type
  key_name                    = var.ssh_key_name
  subnet_id                   = aws_subnet.lab.id
  vpc_security_group_ids      = [aws_security_group.host.id]
  associate_public_ip_address = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = var.root_volume_gib
    delete_on_termination = false
    tags = {
      Name = "${var.lab_name}-retained-root"
    }
  }

  tags = {
    Name = var.lab_name
  }

  lifecycle {
    precondition {
      condition = (
        contains(data.aws_ec2_instance_type.lab.supported_architectures, "x86_64") &&
        data.aws_ec2_instance_type.lab.default_vcpus >= 8 &&
        data.aws_ec2_instance_type.lab.memory_size >= 10240
      )
      error_message = "The owned lab requires x86-64, at least 8 vCPU and at least 10 GiB memory."
    }
  }

  depends_on = [
    aws_route.internet,
    aws_route_table_association.lab,
    aws_vpc_security_group_ingress_rule.ssh,
    aws_vpc_security_group_egress_rule.https,
  ]
}

output "instance_id" {
  value = aws_instance.lab.id
}

output "public_ipv4" {
  value = aws_instance.lab.public_ip
}

output "retained_root_volume_id" {
  value = aws_instance.lab.root_block_device[0].volume_id
}

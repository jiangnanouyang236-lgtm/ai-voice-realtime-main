use anyhow::{Context, Result};
use cpal::traits::{DeviceTrait, HostTrait};
use cpal::{Device, Host};

pub fn default_host() -> Host {
    cpal::default_host()
}

pub fn find_input_device(name_prefix: Option<&str>) -> Result<Device> {
    let host = default_host();
    match name_prefix {
        Some(prefix) => find_named_input_device(&host, prefix).or_else(|_| {
            tracing::warn!("[AUDIO][WARN] 未找到麦克风 {}，回退默认输入设备", prefix);
            host.default_input_device().context("没有找到默认输入设备")
        }),
        None => host.default_input_device().context("没有找到默认输入设备"),
    }
}

fn find_named_input_device(host: &Host, name_prefix: &str) -> Result<Device> {
    let needle = name_prefix.to_lowercase();
    for device in host.input_devices().context("枚举输入设备失败")? {
        let Ok(name) = device.name() else {
            continue;
        };
        if name.to_lowercase().contains(&needle) {
            tracing::info!("[AUDIO] 使用输入设备: {}", name);
            return Ok(device);
        }
    }
    anyhow::bail!("未找到输入设备: {name_prefix}")
}

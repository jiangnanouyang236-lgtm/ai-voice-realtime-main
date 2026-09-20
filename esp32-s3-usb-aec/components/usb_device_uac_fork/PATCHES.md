# Local UAC fork

This component is based on Espressif `usb_device_uac` 1.2.2 from
`esp-iot-solution` commit `36d8130e8e880720108de2c31ce0779827b1bcd9`.

The local fork is intentional and must remain source controlled. It carries
project fixes that are not present in the stock 1.2.2 component:

- full-duplex speaker and microphone interface descriptors;
- independent speaker and microphone clock entities;
- macOS suspend/resume stream restoration;
- corrected new-play timeout units;
- microphone pacing fixes;
- manual speaker/microphone stream recovery via
  `uac_device_recover_streams()`.
- explicit speaker/microphone interface state notifications via
  `stream_state_cb`, used to reset application buffers on host close/reopen.

Do not replace this directory with a generated `managed_components` copy.
Any upstream upgrade must compare these behaviours and pass USB enumeration,
full-duplex, suspend/resume and long-running playback tests first.

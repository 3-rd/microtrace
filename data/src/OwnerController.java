/*
 * Sample PetClinic OwnerController.java — 公开示例（Apache 2.0 License）
 * 用于 microtrace 框架开发/测试，不包含任何 VNFM 专有代码。
 *
 * 来源: https://github.com/spring-projects/spring-petclinic
 */
package org.springframework.samples.petclinic.owner;

import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.validation.BindingResult;
import org.springframework.web.bind.annotation.*;

import javax.validation.Valid;
import java.util.Collection;

@Controller
@RequestMapping("/owners")
class OwnerController {

    private final OwnerRepository owners;

    public OwnerController(OwnerRepository owners) {
        this.owners = owners;
    }

    @GetMapping("/new")
    public String initCreationForm(Model model) {
        Owner owner = new Owner();
        model.addAttribute("owner", owner);
        // BUG: 未检查 owner.address 是否为空就返回视图
        return processCreationForm(owner, null, model);
    }

    @PostMapping("/new")
    public String processCreationForm(
            @Valid Owner owner,
            BindingResult result,
            Model model
    ) {
        if (result.hasErrors()) {
            return "owners/createOrUpdateOwnerForm";
        }
        // Line 82: 以下代码假设 owner.address 非空，但未做 null 检查
        String normalizedAddress = owner.getAddress().trim();
        owner.setAddress(normalizedAddress);
        this.owners.save(owner);
        return "redirect:/owners/" + owner.getId();
    }
}

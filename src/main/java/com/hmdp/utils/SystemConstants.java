package com.hmdp.utils;

public class SystemConstants {
    // 上传目录：相对路径（相对后端启动工作目录=项目根），指向仓库内 nginx 的静态目录，
    // 效果等价于 <项目根>/nginx-1.18.0/html/hmdp/imgs，不要写死 D:/xxx 绝对路径
    public static final String IMAGE_UPLOAD_DIR = "nginx-1.18.0/html/hmdp/imgs";
    public static final String USER_NICK_NAME_PREFIX = "user_";
    public static final int DEFAULT_PAGE_SIZE = 5;
    public static final int MAX_PAGE_SIZE = 10;
}
